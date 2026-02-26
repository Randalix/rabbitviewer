import logging
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from core.thumbnail_manager import ThumbnailManager
from core.rendermanager import Priority


class WatchdogHandler(FileSystemEventHandler):
    """
    Filesystem event handler that submits render tasks into ThumbnailManager
    at LOW priority for live changes. Initial indexing is handled by
    BackgroundIndexer; this class is exclusively a live-event monitor.
    """
    def __init__(self, thumbnail_manager: ThumbnailManager, watch_paths: list, is_daemon_mode: bool = False):
        super().__init__()
        self.thumbnail_manager = thumbnail_manager
        self._watch_paths = watch_paths
        self.observer = Observer()
        self.is_daemon_mode = is_daemon_mode
        self._ignore_next_mod = set()

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
        """Schedule the observer on all watch_paths for live filesystem monitoring."""
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

    def stop(self):
        """Shut down the observer thread."""
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

        # why: exiftool -overwrite_original does delete-original + rename-tmp,
        # producing a transient FileDeletedEvent for the real path followed by
        # a FileMovedEvent (src=tmp, already caught above).  Suppress all
        # events for the real path while it is in the ignore set and clear the
        # flag so subsequent real events proceed normally.
        if event.src_path in self._ignore_next_mod:
            self._ignore_next_mod.discard(event.src_path)
            logging.debug(f"Watchdog: Ignoring self-inflicted {event.event_type}: {event.src_path}")
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
