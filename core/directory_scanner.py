import os
import logging
import time
import fnmatch
from dataclasses import dataclass, field
from typing import List, Set, Optional
from core.rendermanager import Priority, SourceJob
from core.thumbnail_manager import ThumbnailManager


@dataclass
class ReconcileContext:
    """Mutable context for scan_incremental_reconcile.

    *db_file_set* is mutated during iteration — files found on disk are
    discarded.  After the generator is exhausted, *ghost_files* contains
    DB entries that no longer exist on disk.  *discovered_files* accumulates
    every file found during the walk for post-scan task creation.
    """
    db_file_set: Set[str]
    ghost_files: List[str] = field(default_factory=list)
    discovered_files: List[str] = field(default_factory=list)

class DirectoryScanner:
    """Handles scanning directories for supported image files."""

    def __init__(self, thumbnail_manager: Optional[ThumbnailManager], config_manager=None):
        self.thumbnail_manager = thumbnail_manager
        self.config_manager = config_manager
        self.min_file_size = config_manager.get("min_file_size", 8192) if config_manager else 8192
        self.ignore_patterns = config_manager.get("ignore_patterns", ["._*"]) if config_manager else ["._*"]
        # Cache once — supported formats never change after plugin load.
        self._supported_extensions: Set[str] = (
            set(thumbnail_manager.get_supported_formats()) if thumbnail_manager else set()
        )

    def is_supported_file(self, file_path: str) -> bool:
        """Check if file is supported by any plugin based on its name and extension."""
        filename = os.path.basename(file_path)
        _, ext = os.path.splitext(file_path)

        # Check ignore patterns first (fastest check)
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(filename, pattern):
                logging.debug(f"Skipping file {file_path}: matches ignore pattern '{pattern}'")
                return False

        # Check file extension
        if ext.lower() not in self._supported_extensions:
            return False

        # Check minimum file size — reject tiny files (web icons, favicons, etc.)
        # so they never enter the GUI model.  Without this check the scanner
        # discovers them and sends scan_progress to the GUI, but _passes_pre_checks
        # later rejects them, leaving ghost placeholders with no DB record.
        # why: intentional double-stat with _passes_pre_checks — scanner and task
        # factory run in different contexts; the scanner gate prevents model pollution
        # while the task factory gate handles files submitted outside the scanner path.
        try:
            if os.path.getsize(file_path) < self.min_file_size:
                logging.debug(f"File too small, skipping: {file_path}")
                return False
        except OSError:
            return False

        return True

    def _scan(self, directory_path: str, recursive: bool) -> List[str]:
        """Internal helper to scan a directory."""
        found_files = []
        if not os.path.isdir(directory_path):
            logging.warning(f"Directory to scan does not exist or is not a directory: {directory_path}")
            return found_files

        if recursive:
            for root, _, files in os.walk(directory_path):
                for filename in files:
                    full_path = os.path.join(root, filename)
                    if self.is_supported_file(full_path):
                        found_files.append(full_path)
        else:
            try:
                for filename in os.listdir(directory_path):
                    full_path = os.path.join(directory_path, filename)
                    if os.path.isfile(full_path) and self.is_supported_file(full_path):
                        found_files.append(full_path)
            except OSError as e:
                logging.error(f"Error scanning directory {directory_path}: {e}")

        return found_files

    def scan_directory(self, directory_path: str, priority: Priority = Priority.GUI_REQUEST_LOW, recursive: bool = True, session_id: str = None) -> None:
        """
        Initiates an asynchronous, incremental scan of a directory by submitting a SourceJob.
        """
        if not self.thumbnail_manager:
            logging.warning("DirectoryScanner: ThumbnailManager not available, cannot start scan job.")
            return

        job_id = f"gui_scan::{session_id or 'default'}::{directory_path}"
        logging.info(f"Submitting scan job '{job_id}' with priority {priority.name}")

        job = SourceJob(
            priority=priority,
            job_id=job_id,
            generator=self.scan_incremental(directory_path, recursive),
            task_factory=self.thumbnail_manager.create_tasks_for_file
        )

        self.thumbnail_manager.render_manager.submit_source_job(job)

    def scan_incremental(self, directory_path: str, recursive: bool = True, batch_size: int = 10):
        """
        Generator that incrementally yields batches of supported file paths.
        Batching reduces priority-queue and notification overhead.
        """
        logging.info(f"Performing incremental scan for: {directory_path} (Recursive: {recursive}, batch_size={batch_size})")
        current_batch = []
        total_yielded = 0
        scan_start = time.monotonic()

        if not os.path.isdir(directory_path):
            logging.warning(f"Directory to scan does not exist: {directory_path}")
            return

        try:
            walk_start = time.monotonic()
            walker = os.walk(directory_path) if recursive else [(directory_path, [], os.listdir(directory_path))]
            for root, _, files in walker:
                walk_elapsed = time.monotonic() - walk_start
                logging.info(f"[chunking] scan_incremental: entering dir '{root}' ({len(files)} entries, {walk_elapsed:.3f}s since last yield/start)")
                for filename in files:
                    try:
                        full_path = os.path.join(root, filename)
                        if os.path.isfile(full_path) and self.is_supported_file(full_path):
                            current_batch.append(full_path)
                            if len(current_batch) >= batch_size:
                                total_yielded += len(current_batch)
                                elapsed = time.monotonic() - scan_start
                                logging.info(f"[chunking] scan_incremental: yielding batch of {len(current_batch)} (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
                                yield current_batch
                                current_batch = []
                                walk_start = time.monotonic()
                    except OSError as e:
                        logging.debug(f"[chunking] scan_incremental: OSError on '{filename}': {e}")
                        continue
            if current_batch:
                total_yielded += len(current_batch)
                elapsed = time.monotonic() - scan_start
                logging.info(f"[chunking] scan_incremental: yielding final batch of {len(current_batch)} (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
                yield current_batch
            elapsed = time.monotonic() - scan_start
            logging.info(f"[chunking] scan_incremental: generator exhausting for '{directory_path}' (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
        except Exception as e:  # why: os.walk can raise PermissionError or unexpected filesystem errors; must not abort the generator and stall the SourceJob
            logging.error(f"Error during directory scan of {directory_path}: {e}", exc_info=True)

    def scan_incremental_reconcile(self, directory_path: str, recursive: bool,
                                     ctx: ReconcileContext, batch_size: int = 10):
        """Like scan_incremental but also tracks ghost files via *ctx*.

        Wraps scan_incremental: for each discovered file, discards it from
        ctx.db_file_set.  After the walk finishes, any paths remaining in
        db_file_set are ghost files (in DB but deleted on disk).
        """
        for batch in self.scan_incremental(directory_path, recursive, batch_size):
            for f in batch:
                ctx.db_file_set.discard(f)
            ctx.discovered_files.extend(batch)
            yield batch

        ctx.ghost_files = list(ctx.db_file_set)

    def scan_single_directory_no_queue(self, directory_path: str) -> List[str]:
        """Scans a single directory non-recursively and returns supported files."""
        return self._scan(directory_path, recursive=False)
