import logging
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from core.thumbnail_manager import ThumbnailManager
from core.rendermanager import Priority, SourceJob
import threading
from typing import Generator, List, Optional

_INITIAL_SCAN_BATCH_SIZE = 100


class WatchdogHandler(FileSystemEventHandler):
    """
    Filesystem event handler that submits render tasks into ThumbnailManager
    at LOW priority for live changes and BACKGROUND_SCAN for the initial sweep.
    """
    def __init__(self, thumbnail_manager: ThumbnailManager, watch_paths: list, is_daemon_mode: bool = False):
        super().__init__()
        self.thumbnail_manager = thumbnail_manager
        self._watch_paths = watch_paths
        self.observer = Observer()
        self.is_daemon_mode = is_daemon_mode
        self._ignore_next_mod = set()
        self.initial_scan_timer: Optional[threading.Timer] = None

    @property
    def watch_paths(self):
        return self._watch_paths

    @watch_paths.setter
    def watch_paths(self, new_paths: list):
        """Setter for watch_paths, automatically restarts the observer."""
        if set(self._watch_paths) != set(new_paths):
            logging.info(f"Watch paths changed from {self._watch_paths} to {new_paths}. Restarting observer.")
            self._watch_paths = new_paths
            self.stop()
            self.start()

    def start(self):
        """Schedule the observer on all watch_paths and arm the delayed initial-scan job."""
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=1.0)
            if self.observer.is_alive():
                logging.warning("Previous Watchdog observer thread did not stop gracefully.")
            self.observer = Observer()

        for path in self.watch_paths:
            if not os.path.exists(path):
                logging.warning(f"Watch path does not exist: {path}")
                continue

            self.observer.schedule(self, path=path, recursive=True)
            logging.info(f"Watching {path} for changes...")

        if self.watch_paths:
            self.observer.start()
            logging.info("Watchdog observer started.")
        else:
            logging.info("No watch paths configured, Watchdog observer not started.")

        if self.is_daemon_mode:
            logging.info("Daemon mode detected. Scheduling initial scan source job.")

            def submit_initial_scan():
                initial_scan_job = SourceJob(
                    job_id=f"watchdog::initial_scan::{self.watch_paths[0] if len(self.watch_paths) == 1 else hash(tuple(sorted(self.watch_paths)))}",
                    priority=Priority.BACKGROUND_SCAN,
                    generator=self._initial_scan_generator(),
                    task_factory=self.thumbnail_manager.create_tasks_for_file
                )
                self.thumbnail_manager.render_manager.submit_source_job(initial_scan_job)

            # why: 30s delay avoids startup race between observer thread and daemon socket readiness
            self.initial_scan_timer = threading.Timer(30.0, submit_initial_scan)
            self.initial_scan_timer.start()


    def _initial_scan_generator(self) -> Generator[List[str], None, None]:
        """Yield supported file paths under all watch_paths in batches for the RenderManager pipeline."""
        logging.info("Starting initial scan of existing files...")
        current_batch = []

        try:
            for watch_path in self.watch_paths:
                if not os.path.exists(watch_path):
                    logging.warning(f"Watch path does not exist for initial scan: {watch_path}")
                    continue

                for root, dirs, files in os.walk(watch_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if self.thumbnail_manager.is_format_supported(file_path):
                            current_batch.append(file_path)
                            if len(current_batch) >= _INITIAL_SCAN_BATCH_SIZE:
                                yield current_batch
                                current_batch = []
            if current_batch:
                yield current_batch

            logging.info(f"Initial scan generator finished.")
        except OSError as e:
            logging.error(f"Error during initial scan generator: {e}", exc_info=True)

    def stop(self):
        """Cancel the pending initial-scan timer and shut down the observer thread."""
        if self.initial_scan_timer:
            self.initial_scan_timer.cancel()
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=1.0)
            if self.observer.is_alive():
                logging.warning("Watchdog observer thread did not stop gracefully.")
        logging.info("Watchdog observer stopped.")

    def ignore_next_modification(self, path: str):
        """Suppress the next watchdog modified event for path — used after a self-inflicted EXIF write."""
        logging.debug(f"Watchdog: Will ignore next modification for {path}")
        self._ignore_next_mod.add(path)

    def dispatch(self, event):
        """Route filesystem events to the appropriate render or DB-cleanup task."""
        if event.is_directory:
            return

        # why: exiftool writes via atomic rename through a _exiftool_tmp sidecar; ignore the sidecar event
        if event.src_path.endswith("_exiftool_tmp"):
            logging.debug(f"Watchdog: Ignoring temporary file creation/modification: {event.src_path}")
            return

        if event.event_type == 'modified' and event.src_path in self._ignore_next_mod:
            self._ignore_next_mod.discard(event.src_path)
            try:
                rating = self.thumbnail_manager.metadata_db.get_rating(event.src_path)
                logging.info(f"Rating for '{event.src_path}' confirmed in database: {rating}. Ignoring self-inflicted modification event.")
            except AttributeError:
                logging.warning(f"Watchdog: Could not verify rating for '{event.src_path}'. Ignoring self-inflicted modification.")
            except Exception as e:
                # why: watchdog callbacks run on observer thread; DB error must not crash the observer
                logging.error(f"Watchdog: Error while verifying rating for '{event.src_path}': {e}")
            return

        if event.event_type in ['created', 'modified']:
            file_path = event.src_path
        elif event.event_type == 'moved':
            file_path = event.dest_path
        elif event.event_type == 'deleted':
            logging.debug(f"Watchdog: Submitting deleted task for {event.src_path}")
            self.thumbnail_manager.render_manager.submit_task(
                f"db_cleanup_deleted::{event.src_path}",
                Priority.HIGH,
                self.thumbnail_manager.metadata_db.remove_records,
                [event.src_path],
            )
            return
        else:
            return

        logging.debug(f"Watchdog: Submitting {event.event_type} task for {file_path}")
        try:
            tasks = self.thumbnail_manager.create_tasks_for_file(file_path, Priority.LOW)
        except Exception as e:
            # why: watchdog callbacks run on observer thread; plugin error must not crash the observer
            logging.error(f"Watchdog: Error creating tasks for '{file_path}': {e}", exc_info=True)
            return
        for task in tasks:
            self.thumbnail_manager.render_manager.submit_task(
                task.task_id, task.priority, task.func, *task.args,
                dependencies=task.dependencies, task_type=task.task_type,
                on_complete_callback=task.on_complete_callback, **task.kwargs
            )
