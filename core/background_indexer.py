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
    the filesystem walk for un-walked directories at ``BACKGROUND_SCAN``.
    After the initial pass completes, the watchdog observer handles all
    further changes — no re-scans are performed."""

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

            def _orphan_generator(files: List[str]):
                batch: list = []
                for f in files:
                    batch.append(f)
                    if len(batch) >= 10:
                        yield batch
                        batch = []
                if batch:
                    yield batch

            job = SourceJob(
                job_id=job_id,
                priority=Priority.ORPHAN_SCAN,
                generator=_orphan_generator(incomplete),
                task_factory=self.thumbnail_manager.create_all_tasks_for_file,
            )
            rm.submit_source_job(job)

    # ------------------------------------------------------------------
    #  Phase 2: checkpoint-aware filesystem walk
    # ------------------------------------------------------------------

    def start_indexing(self):
        # Phase 1: recover orphaned files at ORPHAN_SCAN(15).
        self.recover_orphans()

        # Phase 2: walk un-walked directories at BACKGROUND_SCAN(10).
        rm = self.thumbnail_manager.render_manager
        for path in self.watch_paths:
            if not os.path.exists(path):
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
