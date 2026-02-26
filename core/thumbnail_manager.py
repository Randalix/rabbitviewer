
import os
import pathlib
import hashlib
import time
import logging
import fnmatch
import threading
from queue import Full
from typing import Optional, Dict, List, Set, Tuple, Any, Callable
from core.metadata_database import MetadataDatabase
from core.rendermanager import Priority, RenderManager, RenderTask, TaskState, TaskType
from plugins.base_plugin import plugin_registry
from plugins.exiftool_process import shutdown_all as _shutdown_exiftool_processes
from core.event_system import EventSystem, EventType, DaemonNotificationEventData
from network import protocol

logger = logging.getLogger(__name__)


def _get_mount_point(path: str) -> Optional[str]:
    """Return the /Volumes/X mount point for a path, or None for local paths."""
    parts = pathlib.PurePath(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(pathlib.Path(parts[0]) / parts[1] / parts[2])
    return None   # local path — always accessible


class ThumbnailManager:
    def __init__(self, config_manager, metadata_database: MetadataDatabase, watchdog_handler=None, event_system: Optional[EventSystem] = None, num_workers=8):
        self.config_manager = config_manager
        self.metadata_db = metadata_database
        self.event_system = event_system
        self.thumbnail_size = config_manager.get("thumbnail_size", 64)
        self.min_file_size = config_manager.get("min_file_size", 8192)
        self.ignore_patterns = config_manager.get("ignore_patterns", ["._*"])
        
        cache_dir = config_manager.get("cache_dir")
        self.cache_dir = os.path.expanduser(cache_dir)
        self.thumbnail_cache_dir = os.path.join(self.cache_dir, "thumbnails")
        self.image_cache_dir = os.path.join(self.cache_dir, "images")
        
        os.makedirs(self.thumbnail_cache_dir, exist_ok=True)
        os.makedirs(self.image_cache_dir, exist_ok=True)

        self._plugins_dir = os.path.join(os.path.dirname(__file__), '..', 'plugins')
        self.plugin_registry = plugin_registry
        self.supported_formats: set = set()  # populated by load_plugins()
        
        self.render_manager = RenderManager(num_workers=num_workers)
        self.render_manager.start()
        self.watchdog_handler = watchdog_handler
        self.socket_server = None  # set by rabbitviewer_daemon.py after server construction

        self._volume_cache: Dict[str, Tuple[bool, float]] = {}   # mount_point → (ok, expiry)
        self._volume_cache_lock = threading.Lock()

        self._task_operations: Dict[str, Callable] = {
            "send2trash": self._op_send2trash,
            "remove_records": self._op_remove_records,
        }


    def load_plugins(self) -> None:
        """Load and register all format plugins. Called after the socket is bound."""
        plugin_registry.load_plugins_from_directory(self._plugins_dir, self.cache_dir, self.thumbnail_size)
        self.supported_formats = self.plugin_registry.get_supported_formats()
        logger.info(f"ThumbnailManager supports {len(self.supported_formats)} formats: {sorted(self.supported_formats)}")

    def set_socket_server(self, socket_server_instance):
        """Sets the reference to the ThumbnailSocketServer instance."""
        self.socket_server = socket_server_instance

    def start_chunked_db_cleanup(self, chunk_size: int = 250):
        """
        Initiates a non-blocking, chunked cleanup of stale database records.
        """
        logging.info("Starting chunked database cleanup for missing files...")

        all_paths = self.metadata_db.get_all_file_paths()
        if not all_paths:
            logging.info("No records in database to check.")
            return

        logging.info(f"Checking {len(all_paths)} database records in chunks of {chunk_size}...")

        chunk_count = 0
        for i in range(0, len(all_paths), chunk_size):
            chunk = all_paths[i:i + chunk_size]
            task_id = f"db-cleanup-chunk-{chunk_count}"
            self.render_manager.submit_task(
                task_id=task_id,
                priority=Priority.LOW,
                func=self._check_and_clean_chunk,
                paths_chunk=chunk
            )
            chunk_count += 1
        logging.info(f"Submitted {chunk_count} cleanup chunks to the render queue.")

    def _check_and_clean_chunk(self, paths_chunk: List[str]):
        """
        Worker task that checks a chunk of paths for existence and removes stale records.
        """
        # Sample the first path — chunks are typically single-volume; if the volume
        # is unreachable, skip rather than risk a 2s timeout per path.
        if paths_chunk:
            sample = paths_chunk[0]
            if not self._is_volume_accessible(sample):
                logger.warning("Skipping DB cleanup chunk — volume inaccessible for: %s", sample)
                return

        missing_paths = [path for path in paths_chunk if not os.path.exists(path)]

        if missing_paths:
            logging.debug(f"Found {len(missing_paths)} missing files in chunk. Removing records.")
            self.metadata_db.remove_records(missing_paths)

    def get_thumbnail(self, image_path):
        """
        Synchronously get or generate a thumbnail. Returns the path to the thumbnail.
        This method should be used sparingly, primarily for cases where immediate
        availability is critical and blocking is acceptable (e.g., a single image
        display where the user is waiting). For general grid loading, use request_thumbnail.
        """
        if not os.path.exists(image_path):
            logger.error(f"ThumbnailManager: Image not found: {image_path}")
            return None

        # Check if thumbnail is already valid in DB and exists on disk
        if self.metadata_db.is_thumbnail_valid(image_path):
            paths = self.metadata_db.get_thumbnail_paths(image_path)
            thumbnail_path = paths.get('thumbnail_path')
            if thumbnail_path and os.path.exists(thumbnail_path):
                logger.debug(f"Thumbnail for {image_path} found in cache: {thumbnail_path}")
                return thumbnail_path

        # If not cached or invalid, trigger synchronous generation.
        # This will block until the thumbnail is generated.
        logger.info(f"ThumbnailManager: Synchronously generating thumbnail and metadata for {image_path}")

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for {image_path}")
            return None
        md5_hash = self._hash_file(image_path)
        if not md5_hash:
            return None

        thumbnail_path = plugin.process_thumbnail(image_path, md5_hash)

        if thumbnail_path:
            self.metadata_db.set_thumbnail_paths(image_path, thumbnail_path=thumbnail_path)
            logger.debug(f"Sync thumbnail for {image_path} done. Queueing followup tasks.")
            view_task_id = f"view::{image_path}"
            self.render_manager.submit_task(
                view_task_id,
                Priority.NORMAL,
                self._process_view_image_task,
                image_path, md5_hash
            )

            metadata_task_id = f"meta::{image_path}"
            self.render_manager.submit_task(
                metadata_task_id,
                Priority.LOW,
                self._process_metadata_task,
                image_path
            )
            return thumbnail_path
        else:
            logger.error(f"Synchronous thumbnail generation failed for {image_path}")
            return None

    def _passes_pre_checks(self, image_path: str) -> bool:
        """
        Performs pre-checks (existence, ignore patterns, file size, format support)
        before queuing a thumbnail generation task.
        """
        if not os.path.isfile(image_path):
            logger.debug(f"Path is not a regular file, skipping: {image_path}")
            return False

        filename = os.path.basename(image_path)
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(filename, pattern):
                logger.debug(f"File matches ignore pattern, skipping: {image_path}")
                return False

        try:
            file_size = os.path.getsize(image_path)
            if file_size < self.min_file_size:
                logger.debug(f"File too small, skipping: {image_path} ({file_size} bytes)")
                return False
        except OSError as e:
            logger.warning(f"Could not get size for file {image_path}, skipping: {e}")
            return False

        if not self.is_format_supported(image_path):
            logger.debug(f"Unsupported file format, skipping: {image_path}")
            return False
        
        return True

    def _generate_thumbnail_task(self, image_path: str, expected_session_id: Optional[str] = None):
        """
        Worker task (Stage A/B): generates the embedded thumbnail only (~1-2s on NAS).
        Sends previews_ready immediately on success. No view image is generated here.

        expected_session_id is accepted for API compatibility with the priority-upgrade
        path in request_thumbnail but is intentionally not checked — thumbnails are fast
        enough that we always complete them so the result is cached for the next session.
        """
        if not os.path.exists(image_path):
            logger.warning(f"File not found during thumbnail processing: '{image_path}'. Queuing JIT database cleanup.")
            self.render_manager.submit_task(
                f"jit-cleanup::{image_path}",
                Priority.HIGH,
                self.metadata_db.remove_records,
                [image_path]
            )
            raise FileNotFoundError(f"Original file not found, record will be cleaned up: {image_path}")

        if not self._is_volume_accessible(image_path):
            return None

        # Re-check validity — another task may have already processed this file.
        if self.metadata_db.is_thumbnail_valid(image_path):
            logger.debug(f"Thumbnail for {image_path} already valid. Sending notification and skipping.")
            paths = self.metadata_db.get_thumbnail_paths(image_path)
            notification_data = protocol.PreviewsReadyData(
                image_entry=protocol.ImageEntryModel(path=image_path),
                thumbnail_path=paths.get('thumbnail_path'),
                view_image_path=paths.get('view_image_path')
            )
            notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
            try:
                self.render_manager.notification_queue.put_nowait(notification)
            except Full:
                logger.warning("Notification queue full; dropping previews_ready for %s", image_path)
            return paths.get('thumbnail_path')

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for: {image_path}")
            return None

        header_result = self._read_file_header(image_path)
        if not header_result:
            return None
        md5_hash, prefetch_buffer = header_result

        # Pass the already-read header buffer so plugins can avoid a second NAS
        # read for orientation and thumbnail extraction.
        thumbnail_path = plugin.process_thumbnail(image_path, md5_hash, prefetch_buffer=prefetch_buffer)
        if thumbnail_path:
            self.metadata_db.set_thumbnail_paths(image_path, thumbnail_path=thumbnail_path)
        else:
            logger.error(f"Thumbnail generation failed for {image_path}.")

        # Send notification immediately — do not wait for the view image (Stage C).
        # Include view_image_path if it already exists from a prior run.
        existing_view = self.metadata_db.get_thumbnail_paths(image_path).get('view_image_path')
        notification_data = protocol.PreviewsReadyData(
            image_entry=protocol.ImageEntryModel(path=image_path),
            thumbnail_path=thumbnail_path,
            view_image_path=existing_view
        )
        notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
        try:
            self.render_manager.notification_queue.put_nowait(notification)
        except Full:
            logger.warning("Notification queue full; dropping previews_ready for %s", image_path)

        return thumbnail_path

    def _generate_view_image_task(self, image_path: str, expected_session_id: Optional[str] = None,
                                    cancel_event: Optional[threading.Event] = None):
        """Worker task (Stage C): generates the full-resolution view image.

        Aborts before the expensive exiftool call if *expected_session_id* is
        stale or *cancel_event* is set.
        """
        if not os.path.exists(image_path):
            logger.warning(f"File not found during view image processing: '{image_path}'. Queuing JIT database cleanup.")
            self.render_manager.submit_task(
                f"jit-cleanup::{image_path}",
                Priority.HIGH,
                self.metadata_db.remove_records,
                [image_path]
            )
            raise FileNotFoundError(f"Original file not found: {image_path}")

        if not self._is_volume_accessible(image_path):
            return None

        # Re-check: view image may already exist from a previous run.
        current_paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = current_paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):
            logger.debug(f"View image for {image_path} already exists. Skipping.")
            return existing_view

        # Session guard: skip expensive work if the user has navigated away.
        if (expected_session_id is not None
                and self.socket_server is not None
                and self.socket_server.active_gui_session_id != expected_session_id):
            logger.debug(
                f"Session changed ({expected_session_id[:8]}→"
                f"{str(self.socket_server.active_gui_session_id)[:8]}), "
                f"skipping view-image for: {os.path.basename(image_path)}"
            )
            return None

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for: {image_path}")
            return None

        md5_hash = self._hash_file(image_path)
        if not md5_hash:
            return None

        if cancel_event and cancel_event.is_set():
            return None

        # Slow step: exiftool -JpgFromRaw, 7-17s per CR3 on NAS.
        view_image_path = self._process_view_image_task(image_path, md5_hash)
        if not view_image_path:
            logger.error(f"View image generation failed for {image_path}.")
            return None

        # Send final notification with both paths now available.
        thumbnail_path = self.metadata_db.get_thumbnail_paths(image_path).get('thumbnail_path')
        notification_data = protocol.PreviewsReadyData(
            image_entry=protocol.ImageEntryModel(path=image_path),
            thumbnail_path=thumbnail_path,
            view_image_path=view_image_path
        )
        notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
        try:
            self.render_manager.notification_queue.put_nowait(notification)
        except Full:
            logger.warning("Notification queue full; dropping previews_ready (view image) for %s", image_path)

        return view_image_path

    def request_thumbnail(self, image_path: str, priority: Priority,
                          gui_session_id: Optional[str] = None) -> bool:
        """
        Asynchronously request a thumbnail generation using the RenderManager.
        This method is now primarily for upgrading task priorities, not creating them.
        The actual task creation is handled once by the 'gui_scan_tasks' SourceJob.

        gui_session_id: the active GUI session at the time of the request.  It is
        stamped onto preview tasks so that _generate_previews_task can abort early
        (before the expensive view-image step) if the user navigates away.

        Returns:
            True if request was queued successfully, False otherwise.
        """
        if not image_path:
            return False

        # Fast path: thumbnail cached locally — notify immediately without
        # stat-ing the source file.  Staleness is handled by the deferred
        # reconcile walk which re-validates mtime/size in the background.
        cached = self.metadata_db.get_cached_thumbnail_paths(image_path)
        if cached and cached.get('thumbnail_path'):
            notification_data = protocol.PreviewsReadyData(
                image_entry=protocol.ImageEntryModel(path=image_path),
                thumbnail_path=cached['thumbnail_path'],
                view_image_path=cached.get('view_image_path')
            )
            notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
            try:
                self.render_manager.notification_queue.put_nowait(notification)
            except Full:
                logger.warning("Notification queue full; dropping previews_ready notification for %s", image_path)
            return True

        # Slow path: check whether the task already exists in the graph.
        # If it does, upgrade its priority. If not (the background scanner hasn't
        # reached this file yet), create it immediately at the requested priority
        # so the GUI doesn't stall waiting for the generator to arrive in order.
        task_id = image_path
        with self.render_manager.graph_lock:
            task_exists = task_id in self.render_manager.task_graph
            if task_exists and gui_session_id:
                # Stamp expected_session_id onto the pending preview task so that
                # _generate_previews_task can detect a session change before its
                # expensive view-image step and abort early, freeing the worker.
                preview_task = self.render_manager.task_graph.get(task_id)
                if (preview_task is not None
                        and preview_task.state not in (TaskState.RUNNING,
                                                       TaskState.COMPLETED,
                                                       TaskState.FAILED)):
                    preview_task.kwargs['expected_session_id'] = gui_session_id

        if task_exists:
            tasks_to_upgrade = {f"meta::{image_path}", task_id}
            self.render_manager.update_task_priorities(tasks_to_upgrade, priority)
            logger.debug(f"ThumbnailManager: Upgraded priority to {priority.name} for: {image_path}")
        else:
            # Task hasn't been created by the background scanner yet.
            # Submit tasks directly without going through create_tasks_for_file —
            # that function calls _passes_pre_checks which does blocking stat calls
            # (os.path.isfile, os.path.getsize) that are slow on network storage.
            # Running those on the socket handler thread serialises them and stalls
            # the handler for every visible image. The task functions themselves
            # re-check validity when executed by a worker thread.
            self.render_manager.submit_task(
                image_path, priority, self._generate_thumbnail_task, image_path,
                expected_session_id=gui_session_id
            )
            self.render_manager.submit_task(
                f"meta::{image_path}", priority, self._process_metadata_task, image_path
            )
            logger.debug(f"ThumbnailManager: Submitted on-demand tasks at {priority.name} for: {image_path}")
        return True

    def batch_request_thumbnails(self, image_paths: List[str], priority: Priority,
                                  gui_session_id: Optional[str] = None) -> int:
        """
        Batch version of request_thumbnail.  Checks thumbnail validity for all
        paths in a single DB query, then upgrades or submits tasks with minimal
        lock contention.

        Returns the number of paths successfully queued or notified.
        """
        if not image_paths:
            return 0

        # Single DB query for all paths — trust-cache, no source file stat.
        validity = self.metadata_db.batch_get_cached_thumbnail_validity(image_paths)

        # Separate cached (valid) from uncached paths.
        cached_paths = []
        uncached_paths = []
        for path in image_paths:
            info = validity.get(path)
            if info and info['valid']:
                cached_paths.append((path, info))
            else:
                uncached_paths.append(path)

        # Batch-notify for all cached thumbnails.
        for path, info in cached_paths:
            notification = protocol.Notification(
                type="previews_ready",
                data=protocol.PreviewsReadyData(
                    image_entry=protocol.ImageEntryModel(path=path),
                    thumbnail_path=info.get('thumbnail_path'),
                    view_image_path=info.get('view_image_path'),
                ).model_dump()
            )
            try:
                self.render_manager.notification_queue.put_nowait(notification)
            except Full:
                logger.warning("Notification queue full; dropping batch notification for %s", path)

        # For uncached paths, check task graph in a single lock scope.
        tasks_to_upgrade = set()
        paths_to_submit = []
        with self.render_manager.graph_lock:
            for path in uncached_paths:
                if path in self.render_manager.task_graph:
                    tasks_to_upgrade.add(path)
                    tasks_to_upgrade.add(f"meta::{path}")
                    # Stamp session ID for session-aware abort.
                    if gui_session_id:
                        task = self.render_manager.task_graph.get(path)
                        if (task is not None
                                and task.state not in (TaskState.RUNNING,
                                                       TaskState.COMPLETED,
                                                       TaskState.FAILED)):
                            task.kwargs['expected_session_id'] = gui_session_id
                else:
                    paths_to_submit.append(path)

        # Batch-upgrade existing tasks (single call, single lock acquisition).
        if tasks_to_upgrade:
            self.render_manager.update_task_priorities(tasks_to_upgrade, priority)

        # Submit new tasks for paths not yet in the graph.
        for path in paths_to_submit:
            self.render_manager.submit_task(
                path, priority, self._generate_thumbnail_task, path,
                expected_session_id=gui_session_id
            )
            self.render_manager.submit_task(
                f"meta::{path}", priority, self._process_metadata_task, path
            )

        return len(cached_paths) + len(uncached_paths)

    def request_view_image(self, image_path: str,
                           gui_session_id: Optional[str] = None) -> Optional[str]:
        """
        Requests view image generation at FULLRES_REQUEST priority (highest non-shutdown).

        - If the view image is already on disk: returns its path immediately (no task).
        - If a view image task is in the graph: upgrades it to FULLRES_REQUEST.
        - If no task exists yet: submits _generate_view_image_task at FULLRES_REQUEST.

        Returns the cached view image path, or None if generation has been queued.
        """
        if not image_path:
            return None

        # Fast path: view image already cached on disk.
        paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):
            return existing_view

        view_task_id = f"view::{image_path}"

        with self.render_manager.graph_lock:
            task_exists = view_task_id in self.render_manager.task_graph
            if task_exists and gui_session_id:
                view_task = self.render_manager.task_graph.get(view_task_id)
                if (view_task is not None
                        and view_task.state not in (TaskState.RUNNING,
                                                    TaskState.COMPLETED,
                                                    TaskState.FAILED)):
                    view_task.kwargs['expected_session_id'] = gui_session_id

        if task_exists:
            self.render_manager.update_task_priorities(
                {view_task_id}, Priority.FULLRES_REQUEST
            )
            logger.debug(f"ThumbnailManager: Upgraded view image task to FULLRES_REQUEST for: {image_path}")
        else:
            self.render_manager.submit_task(
                view_task_id, Priority.FULLRES_REQUEST,
                self._generate_view_image_task, image_path,
                expected_session_id=gui_session_id
            )
            logger.debug(f"ThumbnailManager: Submitted FULLRES_REQUEST view image task for: {image_path}")

        return None

    def downgrade_thumbnail_tasks(self, image_paths: List[str],
                                   priority: Priority = Priority.GUI_REQUEST_LOW):
        """
        Downgrades thumbnail (and metadata) tasks for images that have scrolled
        out of the visible viewport. Uses the same invalidation + re-queue
        strategy as priority upgrades.
        """
        task_ids: Set[str] = set()
        for path in image_paths:
            task_ids.add(path)              # thumbnail task id
            task_ids.add(f"meta::{path}")   # metadata task id
        self.render_manager.downgrade_task_priorities(task_ids, priority)

    def request_speculative_fullres(self, image_path: str, priority: Priority,
                                     gui_session_id: Optional[str] = None):
        """Submit or upgrade a speculative fullres task for heatmap pre-caching."""
        view_task_id = f"view::{image_path}"

        paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):
            return

        # Only create a new Event if the task doesn't already exist;
        # submit_task preserves the existing cancel_event on upgrade.
        with self.render_manager.graph_lock:
            existing = self.render_manager.task_graph.get(view_task_id)
        evt = existing.cancel_event if existing else threading.Event()

        self.render_manager.submit_task(
            view_task_id, priority,
            self._generate_view_image_task, image_path,
            expected_session_id=gui_session_id,
            cancel_event=evt,
        )

    def cancel_speculative_fullres(self, image_path: str):
        self.render_manager.cancel_task(f"view::{image_path}")

    def cancel_speculative_fullres_batch(self, image_paths: List[str]):
        self.render_manager.cancel_tasks([f"view::{p}" for p in image_paths])

    def _process_view_image_task(self, image_path: str, md5_hash: str):
        logger.debug(f"Starting view image task for {image_path}")
        if not os.path.exists(image_path):
            logger.warning(f"File not found for view image processing: '{image_path}'. Queuing JIT database cleanup.")
            self.render_manager.submit_task(
                f"jit-cleanup::{image_path}",
                Priority.HIGH,
                self.metadata_db.remove_records,
                [image_path]
            )
            return None

        current_paths = self.metadata_db.get_thumbnail_paths(image_path)
        current_view_image_path = current_paths.get('view_image_path')
        if current_view_image_path and os.path.exists(current_view_image_path):
            logger.debug(f"View image for {image_path} already exists at {current_view_image_path}. Skipping generation.")
            return current_view_image_path

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for format: {ext}")
            return None

        start_time = time.time()
        view_image_path = plugin.process_view_image(image_path, md5_hash)
        duration = time.time() - start_time
        logger.debug(f"plugin.process_view_image for {os.path.basename(image_path)} took {duration:.4f} seconds.")
        if view_image_path:
            self.metadata_db.set_thumbnail_paths(image_path, view_image_path=view_image_path)
        return view_image_path

    def _process_metadata_task(self, image_path: str):
        """Fast metadata scan (orientation, rating, file_size).
        Queues a deferred full exiftool extraction at BACKGROUND_SCAN."""
        logger.debug(f"Starting fast metadata extraction for {image_path}")
        if not os.path.exists(image_path):
            logger.warning(f"File not found for metadata extraction: '{image_path}'. Queuing JIT database cleanup.")
            self.render_manager.submit_task(
                f"jit-cleanup::{image_path}",
                Priority.HIGH,
                self.metadata_db.remove_records,
                [image_path]
            )
            return

        if not self._is_volume_accessible(image_path):
            return

        start_time = time.time()
        self.metadata_db.extract_and_store_fast_metadata(image_path)
        duration = time.time() - start_time
        logger.debug(f"Fast metadata for {os.path.basename(image_path)} took {duration:.4f}s")

        if self.metadata_db.needs_full_metadata(image_path):
            self.render_manager.submit_task(
                f"meta_full::{image_path}",
                Priority.BACKGROUND_SCAN,
                self._process_full_metadata_task,
                image_path,
            )

    def _process_full_metadata_task(self, image_path: str):
        if not os.path.exists(image_path):
            return
        if not self._is_volume_accessible(image_path):
            return
        # Re-check: another worker may have completed this between scheduling and execution
        if not self.metadata_db.needs_full_metadata(image_path):
            return

        start_time = time.time()
        self.metadata_db.extract_and_store_full_metadata(image_path)
        duration = time.time() - start_time
        logger.debug(f"Full metadata for {os.path.basename(image_path)} took {duration:.4f}s")

    def write_rating_to_file(self, file_path: str, rating: int):
        """
        Finds the correct plugin and uses it to write the rating to an XMP sidecar.
        This method is intended to be called by the RenderManager.
        Returns True on success, False on failure.
        """
        from plugins.base_plugin import sidecar_path_for
        if self.watchdog_handler:
            self.watchdog_handler.ignore_next_modification(sidecar_path_for(file_path))

        if not os.path.exists(file_path):
            logger.warning(f"File not found, cannot write rating: {file_path}")
            return False

        ext = os.path.splitext(file_path)[1].lower()
        plugin = self.plugin_registry.get_plugin_for_format(ext)

        if plugin and plugin.is_available():
            success = plugin.write_rating(file_path, rating)
            if not success:
                logger.error(f"Plugin failed to write rating for {file_path}")
            return success

        logger.warning(f"No plugin found or available for format {ext} to write rating for {file_path}")
        return False

    def write_tags_to_file(self, file_path: str, tag_names: list):
        """Writes the full tag list to the file's XMP:Subject via the appropriate plugin.

        Mirrors write_rating_to_file: watchdog suppression, plugin lookup, exiftool write.
        """
        from plugins.base_plugin import sidecar_path_for
        if self.watchdog_handler:
            self.watchdog_handler.ignore_next_modification(sidecar_path_for(file_path))

        if not os.path.exists(file_path):
            logger.warning(f"File not found, cannot write tags: {file_path}")
            return False

        ext = os.path.splitext(file_path)[1].lower()
        plugin = self.plugin_registry.get_plugin_for_format(ext)

        if plugin and plugin.is_available():
            success = plugin.write_tags(file_path, tag_names)
            if not success:
                logger.error(f"Plugin failed to write tags for {file_path}")
            return success

        logger.warning(f"No plugin found or available for format {ext} to write tags for {file_path}")
        return False

    def get_cached_thumbnail_path(self, md5_hash: str) -> str:
        """Get the path where a thumbnail should be stored."""
        return os.path.join(self.thumbnail_cache_dir, f"{md5_hash}.jpg")
    
    def get_cached_paths(self, image_path: str) -> Optional[Dict[str, str]]:
        """Get cached thumbnail and full resolution paths for an image."""
        paths = self.metadata_db.get_thumbnail_paths(image_path)
        if paths['thumbnail_path'] or paths['view_image_path']:
            return {
                'thumbnail_path': paths['thumbnail_path'],
                'full_res_path': paths['view_image_path'] if paths['view_image_path'] else image_path
            }
        return None
    
    def _hash_and_process_view_image(self, image_path: str):
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")
            
        md5_hash = self._hash_file(image_path)
        if not md5_hash:
            raise ValueError(f"Could not hash file: {image_path}")
        return self._process_view_image_task(image_path, md5_hash)

    def is_format_supported(self, image_path: str) -> bool:
        _, ext = os.path.splitext(image_path)
        return self.plugin_registry.get_plugin_for_format(ext.lower()) is not None
    
    def get_supported_formats(self) -> List[str]:
        return list(self.supported_formats)

    def _is_volume_accessible(self, path: str, timeout: float = 2.0) -> bool:
        """
        Returns False if the volume containing *path* does not respond within
        *timeout* seconds. Results are cached per mount point for 60 s.
        Local paths always return True without probing.
        Callers that return early on False do not requeue the skipped task;
        the file will be processed again only on the next scan or watchdog event.
        """
        mount_point = _get_mount_point(path)
        if mount_point is None:
            return True

        now = time.time()
        with self._volume_cache_lock:
            cached = self._volume_cache.get(mount_point)
            if cached is not None and now < cached[1]:
                return cached[0]

        responded = threading.Event()
        def _probe():
            try:
                os.stat(mount_point)
                responded.set()
            except OSError:
                pass   # event stays unset; timeout path handles it

        threading.Thread(target=_probe, daemon=True).start()
        accessible = responded.wait(timeout)

        with self._volume_cache_lock:
            self._volume_cache[mount_point] = (accessible, now + 60.0)

        if not accessible:
            logger.warning("Volume inaccessible (timeout %.1fs): %s — skipping task.", timeout, mount_point)
        return accessible

    def _hash_file(self, file_path: str) -> Optional[str]:
        """Generate MD5 hash of the first 256KB of the file for performance.
        Reads only 256KB — callers that also need the prefetch buffer should
        call _read_file_header directly to avoid a second I/O round-trip.
        """
        result = self._read_file_header(file_path, prefetch_size=256 * 1024)
        return result[0] if result else None

    def _read_file_header(self, file_path: str, prefetch_size: int = 512 * 1024) -> Optional[Tuple[str, bytes]]:
        """
        Read the first *prefetch_size* bytes of *file_path* in a single syscall.

        Returns ``(md5_of_first_256KB, header_bytes)`` so callers can both
        identify the file and inspect its binary structure without a second NAS
        round-trip.  Returns ``None`` on error.
        """
        if os.path.isdir(file_path):
            logger.warning(f"Cannot hash a directory: {file_path}. Skipping.")
            return None

        start_time = time.time()
        try:
            with open(file_path, "rb") as f:
                header = f.read(prefetch_size)

            # Hash only the first 256 KB so the digest stays compatible with
            # thumbnails already on disk from previous runs.
            hash_chunk = header[:256 * 1024]
            md5 = hashlib.md5(hash_chunk).hexdigest()

            duration = time.time() - start_time
            logger.debug(f"read_file_header {os.path.basename(file_path)}: {len(header)} B in {duration:.4f}s")
            return md5, header
        except OSError as e:
            duration = time.time() - start_time
            logger.error(f"ThumbnailManager: Error reading header of {file_path} after {duration:.4f}s: {e}")
            return None

    def check_thumbnails_status(self, image_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """Check the status of multiple thumbnails using database cache."""
        statuses = {}
        for image_path in image_paths:
            if not os.path.exists(image_path):
                statuses[image_path] = {"ready": False, "error": "File not found"}
                continue
                
            if self.metadata_db.is_thumbnail_valid(image_path):
                paths = self.metadata_db.get_thumbnail_paths(image_path)
                thumbnail_path = paths.get('thumbnail_path')
                
                if thumbnail_path and os.path.exists(thumbnail_path):
                    statuses[image_path] = {
                        "ready": True,
                        "path": thumbnail_path
                    }
                else:
                    statuses[image_path] = {"ready": False}
                
        return statuses

    def request_metadata_extraction(self, image_paths: List[str], priority: Priority = Priority.NORMAL):
        """Submit or upgrade metadata extraction tasks for a list of images."""
        logger.info(f"Queueing metadata extraction for {len(image_paths)} images with {priority.name} priority.")
        for image_path in image_paths:
            if os.path.exists(image_path):
                self.render_manager.submit_task(
                    f"meta::{image_path}",
                    priority,
                    self._process_metadata_task,
                    image_path,
                )

    def create_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """
        Task Factory: Creates all necessary tasks for a single file with correct dependencies.
        Returns a list of tasks to be submitted to the RenderManager.
        """
        if not self._passes_pre_checks(file_path):
            return []

        if self.metadata_db.is_thumbnail_valid(file_path):
            logger.debug(f"Previews for {file_path} already valid. No tasks created.")
            # Notify the GUI for any GUI-initiated scan (slow scan runs at GUI_REQUEST_LOW).
            if priority >= Priority.GUI_REQUEST_LOW:
                paths = self.metadata_db.get_thumbnail_paths(file_path)
                notification_data = protocol.PreviewsReadyData(
                    image_entry=protocol.ImageEntryModel(path=file_path),
                    thumbnail_path=paths.get('thumbnail_path'),
                    view_image_path=paths.get('view_image_path')
                )
                notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
                try:
                    self.render_manager.notification_queue.put_nowait(notification)
                except Full:
                    logger.warning("Notification queue full; dropping previews_ready for %s", file_path)
            return []

        # Establish a baseline priority for new thumbnails. All thumbnails from a background
        # scan start at a low priority, allowing the GUI to promote visible ones to
        # a much higher priority (GUI_REQUEST) for maximum responsiveness.
        base_priority = priority
 
        meta_id = f"meta::{file_path}"
        thumb_id = file_path

        meta_task = RenderTask(
            task_id=meta_id,
            priority=base_priority,
            func=self._process_metadata_task,
            args=(file_path,)
        )

        # Stage C (view image) is handled by a separate SourceJob.
        thumb_task = RenderTask(
            task_id=thumb_id,
            priority=base_priority,
            func=self._generate_thumbnail_task,
            args=(file_path,)
        )
        return [meta_task, thumb_task]

    def create_view_image_task_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """
        Task Factory for Stage C: creates a view image generation task for a single file.
        Returns an empty list if the view image already exists on disk or the file is
        not a supported format (so the Stage C SourceJob silently skips those files).
        """
        if not self._passes_pre_checks(file_path):
            return []

        paths = self.metadata_db.get_thumbnail_paths(file_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):
            logger.debug(f"View image for {file_path} already exists. No Stage C task created.")
            return []

        view_task = RenderTask(
            task_id=f"view::{file_path}",
            priority=priority,
            func=self._generate_view_image_task,
            args=(file_path,)
        )
        return [view_task]

    def create_all_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """Task factory for daemon background indexing: creates thumbnail, metadata,
        and view image tasks in a single pass — one ``_passes_pre_checks`` call and
        one DB lookup instead of two."""
        if not self._passes_pre_checks(file_path):
            return []

        tasks: List[RenderTask] = []

        if not self.metadata_db.is_thumbnail_valid(file_path):
            tasks.append(RenderTask(
                task_id=f"meta::{file_path}",
                priority=priority,
                func=self._process_metadata_task,
                args=(file_path,),
            ))
            tasks.append(RenderTask(
                task_id=file_path,
                priority=priority,
                func=self._generate_thumbnail_task,
                args=(file_path,),
            ))

        paths = self.metadata_db.get_thumbnail_paths(file_path)
        existing_view = paths.get('view_image_path') if paths else None
        if not (existing_view and os.path.exists(existing_view)):
            tasks.append(RenderTask(
                task_id=f"view::{file_path}",
                priority=priority,
                func=self._generate_view_image_task,
                args=(file_path,),
            ))

        return tasks

    def create_gui_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """Task factory for GUI directory loads.

        Like create_all_tasks_for_file but assigns view-image tasks at
        BACKGROUND_SCAN regardless of *priority*, keeping view-image work
        below thumbnail generation in the queue.  Warm-cache files emit a
        previews_ready notification immediately (no tasks created).
        """
        if not self._passes_pre_checks(file_path):
            return []

        tasks: List[RenderTask] = []
        thumb_valid = self.metadata_db.is_thumbnail_valid(file_path)

        if thumb_valid:
            # Warm cache: no thumbnail/metadata tasks needed.
            # Don't send previews_ready here — the GUI's heatmap will call
            # request_thumbnail() which handles cache-hit notifications in
            # the correct priority order (cursor-outward).
            pass
        else:
            tasks.append(RenderTask(
                task_id=f"meta::{file_path}",
                priority=priority,
                func=self._process_metadata_task,
                args=(file_path,),
            ))
            tasks.append(RenderTask(
                task_id=file_path,
                priority=priority,
                func=self._generate_thumbnail_task,
                args=(file_path,),
            ))

        # View-image at BACKGROUND_SCAN — runs only after thumbnail queue drains.
        paths = self.metadata_db.get_thumbnail_paths(file_path)
        existing_view = paths.get('view_image_path') if paths else None
        if not (existing_view and os.path.exists(existing_view)):
            tasks.append(RenderTask(
                task_id=f"view::{file_path}",
                priority=Priority.BACKGROUND_SCAN,
                func=self._generate_view_image_task,
                args=(file_path,),
            ))

        return tasks

    # ──────────────────────────────────────────────────────────────────────
    #  Generic task operations (daemon-side registry)
    # ──────────────────────────────────────────────────────────────────────

    def get_task_operation(self, name: str) -> Optional[Callable]:
        return self._task_operations.get(name)

    def execute_compound_task(self, operations: List[Tuple[str, List[str]]]) -> Dict[str, Any]:
        """Execute a sequence of named operations. Runs in a RenderManager worker thread."""
        results: Dict[str, Any] = {}
        for name, file_paths in operations:
            handler = self._task_operations.get(name)
            if not handler:
                logger.error(f"Unknown task operation: {name}")
                results[name] = {"error": f"unknown operation: {name}"}
                continue
            try:
                results[name] = handler(file_paths)
            except Exception as e:  # why: task operations are user-registered handlers; any exception must not crash the worker loop
                logger.error(f"Task operation '{name}' failed: {e}", exc_info=True)
                results[name] = {"error": str(e)}
        return results

    def _op_send2trash(self, file_paths: List[str]) -> Dict[str, Any]:
        """Move files (and their XMP sidecars) to system trash."""
        from core.file_ops import trash_with_sidecars
        return trash_with_sidecars(file_paths)

    def _op_remove_records(self, file_paths: List[str]) -> Dict[str, Any]:
        """Remove database records and associated cache files."""
        success = self.metadata_db.remove_records(file_paths)
        return {"success": success, "count": len(file_paths)}

    def shutdown(self) -> None:
        """Gracefully shuts down the ThumbnailManager and its associated RenderManager."""
        logger.info("ThumbnailManager: Shutting down.")
        self.render_manager.shutdown()
        _shutdown_exiftool_processes()
        logger.info("ThumbnailManager: Shutdown complete.")

    def queue_exif_rating_write(self, file_path: str, rating: int):
        """
        Updates the DB synchronously then queues a task to write rating to EXIF.
        Use this for single-file writes where no prior batch_set_ratings was done.
        """
        self.metadata_db.set_rating(file_path, rating)

        task_id = f"exif_rating::{file_path}"
        logger.debug(f"Queuing EXIF rating write for {file_path} with task ID {task_id}")

        self.render_manager.submit_task(
            task_id=task_id,
            priority=Priority.LOW,
            func=self.write_rating_to_file,
            file_path=file_path,
            rating=rating
        )

    def _handle_daemon_notification(self, message: dict):
        """Translates a daemon message and publishes it to the event system."""
        if not self.event_system or not isinstance(message, dict):
            return

        event = DaemonNotificationEventData(
            event_type=EventType.DAEMON_NOTIFICATION,
            source=self.__class__.__name__,
            timestamp=time.time(),
            notification_type=message.get("type"),
            data=message.get("data", {})
        )
        logger.info(f"GUI-side Manager received and will publish notification: {event.notification_type}")
        self.event_system.publish(event)
