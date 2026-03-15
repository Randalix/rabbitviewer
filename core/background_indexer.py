import os
import logging
from typing import List

from core.rendermanager import Priority, SourceJob

logger = logging.getLogger(__name__)


class BackgroundIndexer:
    """Job IDs use ``daemon_idx::`` prefix — no session ID, so GUI disconnect
    cleanup never cancels these jobs.

    On startup the indexer first recovers orphaned files left by a prior GUI
    session (from the scan ledger) at ``ORPHAN_SCAN`` priority, then resumes
    pending work from the ``file_work`` table, and finally walks un-walked
    directories at ``BACKGROUND_SCAN``."""

    def __init__(self, thumbnail_manager, directory_scanner,
                 watch_paths: list[str], metadata_db=None):
        self.thumbnail_manager = thumbnail_manager
        self.directory_scanner = directory_scanner
        self.watch_paths = watch_paths
        self.metadata_db = metadata_db or thumbnail_manager.metadata_db

    # ------------------------------------------------------------------
    #  Phase 1: recover orphaned work from the scan ledger
    # ------------------------------------------------------------------

    def recover_orphans(self) -> None:
        """Submit recovery jobs for files discovered but not processed."""
        rm = self.thumbnail_manager.render_manager
        scan_roots = self.metadata_db.ledger_get_all_scan_roots()

        for scan_root in scan_roots:
            incomplete = self.metadata_db.ledger_get_incomplete(scan_root)
            if not incomplete:
                continue

            job_id = f"orphan_recovery::{scan_root}"
            # submit_source_job deduplicates by job_id — safe to call on every resume.
            with rm.active_jobs_lock:
                if job_id in rm.active_jobs:
                    continue

            logger.info(
                f"BackgroundIndexer: recovering {len(incomplete)} "
                f"orphaned files for {scan_root}"
            )

            job = SourceJob(
                job_id=job_id,
                priority=Priority.ORPHAN_SCAN,
                generator=self._batch_generator(incomplete),
                task_factory=self.thumbnail_manager.create_all_tasks_for_file,
            )
            rm.submit_source_job(job)

    # ------------------------------------------------------------------
    #  Phase 1.5: resume pending work from the file_work ledger
    # ------------------------------------------------------------------

    def resume_pending_work(self) -> None:
        """Submit jobs for work left incomplete by a prior GUI session."""
        rm = self.thumbnail_manager.render_manager
        roots = self.metadata_db.file_work_get_all_roots()
        if not roots:
            return

        for scan_root in roots:
            pending_types = set(self.metadata_db.file_work_get_pending_types(scan_root))
            if not pending_types:
                continue

            # Core work (thumbnail/view_image/metadata) — union all file paths
            core_types = {'thumbnail', 'view_image', 'metadata'} & pending_types
            if core_types:
                core_files: set = set()
                for wt in core_types:
                    core_files.update(
                        self.metadata_db.file_work_get_pending(scan_root, wt)
                    )
                if core_files:
                    job_id = f"daemon_work::core::{scan_root}"
                    with rm.active_jobs_lock:
                        already_active = job_id in rm.active_jobs
                    if not already_active:
                        logger.info(
                            "BackgroundIndexer: resuming %d core tasks for %s",
                            len(core_files), scan_root,
                        )
                        job = SourceJob(
                            job_id=job_id,
                            priority=Priority.ORPHAN_SCAN,
                            generator=self._batch_generator(list(core_files)),
                            task_factory=self.thumbnail_manager.create_all_tasks_for_file,
                        )
                        rm.submit_source_job(job)

            # AI work — delegate to existing submit methods (they do their
            # own skip-checks via DB queries).
            if 'clip' in pending_types:
                files = self.metadata_db.file_work_get_pending(scan_root, 'clip')
                if files:
                    logger.info("BackgroundIndexer: resuming CLIP indexing for %d files in %s",
                                len(files), scan_root)
                    self.thumbnail_manager.submit_clip_indexing_job(scan_root, files)

            if 'face_detect' in pending_types:
                files = self.metadata_db.file_work_get_pending(scan_root, 'face_detect')
                if files:
                    logger.info("BackgroundIndexer: resuming face detection for %d files in %s",
                                len(files), scan_root)
                    self.thumbnail_manager.submit_face_detection_job(scan_root, files)

            if 'auto_orient' in pending_types:
                files = self.metadata_db.file_work_get_pending(scan_root, 'auto_orient')
                if files:
                    logger.info("BackgroundIndexer: resuming auto-orient for %d files in %s",
                                len(files), scan_root)
                    self.thumbnail_manager.submit_auto_orient_job(scan_root, files)

    @staticmethod
    def _batch_generator(files: List[str]):
        batch: list = []
        for f in files:
            batch.append(f)
            if len(batch) >= 10:
                yield batch
                batch = []
        if batch:
            yield batch

    # ------------------------------------------------------------------
    #  Phase 1.75: recover files in DB with no thumbnail
    # ------------------------------------------------------------------

    def _recover_incomplete_files(self) -> None:
        """Submit work for files in the DB that have no thumbnail.

        This catches files that were discovered by a prior scan but whose
        thumbnail tasks never ran (e.g. due to the resume_pending_work
        deadlock fixed in 71cac8c).
        """
        missing = self.metadata_db.get_files_missing_thumbnails(self.watch_paths)
        if not missing:
            return

        logger.info("BackgroundIndexer: %d files in DB missing thumbnails — submitting recovery jobs",
                     len(missing))
        rm = self.thumbnail_manager.render_manager
        job = SourceJob(
            job_id="recover_incomplete",
            priority=Priority.BACKGROUND_SCAN,
            generator=self._batch_generator(missing),
            task_factory=self.thumbnail_manager.create_all_tasks_for_file,
        )
        rm.submit_source_job(job)

    # ------------------------------------------------------------------
    #  Phase 2: checkpoint-aware filesystem walk
    # ------------------------------------------------------------------

    def start_indexing(self):
        # Phase 0: recover pending file writes (rating, orientation, tags).
        recovered = self.thumbnail_manager.recover_pending_writes()
        if recovered:
            logger.info(f"BackgroundIndexer: recovered {recovered} pending file writes")

        # Phase 1: recover orphaned files at ORPHAN_SCAN(15).
        self.recover_orphans()

        # Phase 1.5: resume pending work from file_work ledger.
        self.resume_pending_work()

        # Phase 1.75: recover files in DB that never got thumbnails (e.g.
        # after a prior deadlock prevented task creation).
        self._recover_incomplete_files()

        # Phase 2: walk un-walked directories at BACKGROUND_SCAN(10).
        rm = self.thumbnail_manager.render_manager
        logger.info("BackgroundIndexer: Phase 2 — %d watch_paths to index", len(self.watch_paths))
        for path in self.watch_paths:
            if not os.path.exists(path):  # disk-io: watch path validation
                logger.warning(f"BackgroundIndexer: skipping non-existent watch_path: {path}")
                continue

            skip_dirs = self.metadata_db.ledger_get_walked_dirs(path)
            if skip_dirs:
                logger.info(
                    f"BackgroundIndexer: skipping {len(skip_dirs)} "
                    f"already-walked dirs for {path}"
                )

            def _ledger_batch_cb(paths, _root=path):
                self.metadata_db.ledger_batch_insert(paths, scan_root=_root)
                for wt in ('thumbnail', 'view_image', 'metadata'):
                    self.metadata_db.file_work_batch_insert(paths, wt, scan_root=_root)

            job = SourceJob(
                job_id=f"daemon_idx::{path}",
                priority=Priority.BACKGROUND_SCAN,
                generator=self.directory_scanner.scan_incremental(
                    path, recursive=True, skip_dirs=skip_dirs or None,
                ),
                task_factory=self.thumbnail_manager.create_all_tasks_for_file,
                on_batch_discovered=_ledger_batch_cb,
            )
            rm.submit_source_job(job)
            logger.info(f"BackgroundIndexer: submitted indexing job for {path}")
