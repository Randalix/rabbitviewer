import os
import logging

from core.rendermanager import Priority, SourceJob

logger = logging.getLogger(__name__)


class BackgroundIndexer:
    """Job IDs use ``daemon_idx::`` prefix — no session ID, so GUI disconnect
    cleanup never cancels these jobs.

    Indexes each watch_path exactly once at daemon startup with a single
    ``os.walk`` pass (thumbnails + metadata + view images).  After the initial
    pass completes, the watchdog observer handles all further changes — no
    re-scans are performed."""

    def __init__(self, thumbnail_manager, directory_scanner, watch_paths: list[str]):
        self.thumbnail_manager = thumbnail_manager
        self.directory_scanner = directory_scanner
        self.watch_paths = watch_paths

    def start_indexing(self):
        # why: submit_source_job deduplicates by job_id, so calling twice is a no-op.
        rm = self.thumbnail_manager.render_manager
        for path in self.watch_paths:
            if not os.path.exists(path):
                logger.warning(f"BackgroundIndexer: skipping non-existent watch_path: {path}")
                continue
            job = SourceJob(
                job_id=f"daemon_idx::{path}",
                priority=Priority.BACKGROUND_SCAN,
                generator=self.directory_scanner.scan_incremental(path, recursive=True),
                task_factory=self.thumbnail_manager.create_all_tasks_for_file,
            )
            rm.submit_source_job(job)
            logger.info(f"BackgroundIndexer: submitted indexing job for {path}")
