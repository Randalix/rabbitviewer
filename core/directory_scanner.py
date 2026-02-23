import os
import logging
import fnmatch
from typing import List, Set, Optional
from core.rendermanager import Priority, SourceJob
from core.thumbnail_manager import ThumbnailManager

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

        # The slow os.path.getsize() check has been removed. It is now handled
        # by the asynchronous ThumbnailManager worker for each individual thumbnail task.

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
        logging.info(f"Performing incremental scan for: {directory_path} (Recursive: {recursive})")
        current_batch = []

        if not os.path.isdir(directory_path):
            logging.warning(f"Directory to scan does not exist: {directory_path}")
            return

        try:
            walker = os.walk(directory_path) if recursive else [(directory_path, [], os.listdir(directory_path))]
            for root, _, files in walker:
                for filename in files:
                    try:
                        full_path = os.path.join(root, filename)
                        if os.path.isfile(full_path) and self.is_supported_file(full_path):
                            current_batch.append(full_path)
                            if len(current_batch) >= batch_size:
                                yield current_batch
                                current_batch = []
                    except OSError:
                        continue
            if current_batch:
                yield current_batch
        except Exception as e:
            logging.error(f"Error during directory scan of {directory_path}: {e}", exc_info=True)

    def scan_single_directory_no_queue(self, directory_path: str) -> List[str]:
        """Scans a single directory non-recursively and returns supported files."""
        return self._scan(directory_path, recursive=False)
