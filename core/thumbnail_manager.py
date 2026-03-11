
import os
import pathlib
import hashlib
import stat as stat_mod
import time
import logging
import fnmatch
import threading
from collections import OrderedDict
from typing import Optional, Dict, List, Set, Tuple, Any, Callable
from core.metadata_database import MetadataDatabase
from core.rendermanager import Priority, RenderManager, RenderTask, TaskType
from core.source_cache import SourceExistsCache
from plugins.base_plugin import plugin_registry
from plugins.exiftool_process import shutdown_all as _shutdown_exiftool_processes
from core import notifications as protocol

logger = logging.getLogger(__name__)

# Formats that Qt/PIL can load directly without extraction.
# When orientation == 1, the GUI can display the original file as-is.
_NATIVELY_VIEWABLE = frozenset({
    '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.tif', '.webp',
})


# Bump when cached image generation changes in a way that invalidates
# existing thumbnails/view-images (e.g., orientation handling).
CACHE_VERSION = 1


def _get_mount_point(path: str) -> Optional[str]:
    """Return the /Volumes/X mount point for a path, or None for local paths."""
    parts = pathlib.PurePath(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(pathlib.Path(parts[0]) / parts[1] / parts[2])
    return None   # local path — always accessible


class ThumbnailManager:
    def __init__(self, config_manager, metadata_database: MetadataDatabase, watchdog_handler=None, event_system=None, num_workers=8):
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

        self._check_cache_migration()

        self._plugins_dir = os.path.join(os.path.dirname(__file__), '..', 'plugins')
        self.plugin_registry = plugin_registry
        self.supported_formats: set = set()  # populated by load_plugins()
        
        self.render_manager = RenderManager(num_workers=num_workers)
        self.render_manager.start()
        self.watchdog_handler = watchdog_handler

        self._volume_cache: Dict[str, Tuple[bool, float]] = {}   # mount_point → (ok, expiry)
        self.cache_size_manager = None  # set by daemon after construction
        self._volume_cache_lock = threading.Lock()
        self.source_cache = SourceExistsCache(ttl=30.0)

        # In-memory LRU cache for fast fullres extractions (below threshold → RAM only).
        self._fullres_mem_cache: OrderedDict[str, bytes] = OrderedDict()
        self._fullres_mem_cache_lock = threading.Lock()
        self._fullres_mem_cache_bytes = 0
        max_mb = config_manager.get("fullres_mem_cache_mb", 512)
        self._fullres_mem_cache_max = max_mb * 1024 * 1024
        threshold_ms = config_manager.get("fullres_cache_threshold_ms", 500)
        self._fullres_cache_threshold = threshold_ms / 1000.0

        self._task_operations: Dict[str, Callable] = {
            "send2trash": self._op_send2trash,
            "remove_records": self._op_remove_records,
        }

        # Limit speculative view tasks to 1 concurrent worker so user
        # requests at FULLRES_REQUEST are never starved.
        self._speculative_view_sem = threading.Semaphore(1)


    # -- Cache version migration -----------------------------------------------

    def _check_cache_migration(self):
        """Invalidate cached thumbnails/view-images when CACHE_VERSION changes."""
        version_file = os.path.join(self.cache_dir, "cache_version")

        current_version = 0
        try:
            with open(version_file) as f:  # disk-io: cache version check
                current_version = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            pass

        if current_version >= CACHE_VERSION:
            return

        logger.warning(
            "Cache version %d < %d — invalidating all cached thumbnails and view images.",
            current_version, CACHE_VERSION,
        )

        rows = self.metadata_db.clear_all_thumbnail_paths()
        logger.info("Cache migration: cleared %d DB thumbnail/view_image paths.", rows)

        import shutil
        for d in (self.thumbnail_cache_dir, self.image_cache_dir):
            if os.path.isdir(d):  # disk-io: cache dir cleanup
                shutil.rmtree(d)
                os.makedirs(d, exist_ok=True)
        logger.info("Cache migration: deleted and recreated thumbnail/image cache directories.")

        with open(version_file, "w") as f:  # disk-io: cache version marker
            f.write(str(CACHE_VERSION))
        logger.info("Cache migration complete. Version marker set to %d.", CACHE_VERSION)

    # -- Fullres memory cache (LRU, byte-size bounded) -----------------------

    def _mem_cache_put(self, image_path: str, data: bytes) -> None:
        with self._fullres_mem_cache_lock:
            # Remove old entry if present (size accounting).
            if image_path in self._fullres_mem_cache:
                self._fullres_mem_cache_bytes -= len(self._fullres_mem_cache.pop(image_path))
            self._fullres_mem_cache[image_path] = data
            self._fullres_mem_cache.move_to_end(image_path)
            self._fullres_mem_cache_bytes += len(data)
            # Evict oldest until under budget.
            while self._fullres_mem_cache_bytes > self._fullres_mem_cache_max and self._fullres_mem_cache:
                _, evicted = self._fullres_mem_cache.popitem(last=False)
                self._fullres_mem_cache_bytes -= len(evicted)

    def _mem_cache_get(self, image_path: str) -> Optional[bytes]:
        with self._fullres_mem_cache_lock:
            data = self._fullres_mem_cache.get(image_path)
            if data is not None:
                self._fullres_mem_cache.move_to_end(image_path)
            return data

    def invalidate_mem_cache(self, image_path: str) -> None:
        with self._fullres_mem_cache_lock:
            data = self._fullres_mem_cache.pop(image_path, None)
            if data is not None:
                self._fullres_mem_cache_bytes -= len(data)

    # -----------------------------------------------------------------------

    def load_plugins(self) -> None:
        plugin_registry.load_plugins_from_directory(self._plugins_dir, self.cache_dir, self.thumbnail_size)
        self.supported_formats = self.plugin_registry.get_supported_formats()
        if not self.supported_formats:
            logger.warning("No format plugins loaded — scanning and thumbnailing will be non-functional.")
        else:
            logger.info(f"ThumbnailManager supports {len(self.supported_formats)} formats: {sorted(self.supported_formats)}")

    def get_thumbnail(self, image_path):
        """
        Synchronously get or generate a thumbnail. Returns the path to the thumbnail.
        This method should be used sparingly, primarily for cases where immediate
        availability is critical and blocking is acceptable (e.g., a single image
        display where the user is waiting). For general grid loading, use request_thumbnail.
        """
        if not os.path.exists(image_path):  # disk-io: source existence guard
            logger.error(f"ThumbnailManager: Image not found: {image_path}")
            return None

        # Check if thumbnail is already valid in DB and exists on disk
        if self.metadata_db.is_thumbnail_valid(image_path):
            paths = self.metadata_db.get_thumbnail_paths(image_path)
            thumbnail_path = paths.get('thumbnail_path')
            if thumbnail_path and os.path.exists(thumbnail_path):  # disk-io: cache file check
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

        Uses ``source_cache.stat()`` so files recently stat'd by the directory
        scanner (or another worker task) don't incur a second NAS round-trip.
        """
        filename = os.path.basename(image_path)
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(filename, pattern):
                logger.debug(f"File matches ignore pattern, skipping: {image_path}")
                return False

        if not self.is_format_supported(image_path):
            logger.debug(f"Unsupported file format, skipping: {image_path}")
            return False

        st = self.source_cache.stat(image_path)
        if st is None:
            logger.debug(f"Cannot stat file, skipping: {image_path}")
            return False

        if not stat_mod.S_ISREG(st.st_mode):
            logger.debug(f"Path is not a regular file, skipping: {image_path}")
            return False
        if st.st_size < self.min_file_size:
            logger.debug(f"File too small, skipping: {image_path} ({st.st_size} bytes)")
            return False

        return True

    def _generate_thumbnail_task(self, image_path: str):
        """Worker task (Stage A/B): generates the embedded thumbnail only (~1-2s on NAS).
        Sends previews_ready immediately on success. No view image is generated here.
        """
        if not self.source_cache.exists(image_path):
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
            self.metadata_db.ledger_mark_complete(image_path)
            paths = self.metadata_db.get_thumbnail_paths(image_path)
            notification_data = protocol.PreviewsReadyData(
                image_entry=protocol.ImageEntryModel(path=image_path),
                thumbnail_path=paths.get('thumbnail_path'),
                view_image_path=paths.get('view_image_path')
            )
            notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
            self.render_manager.notify(notification)
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
            self.metadata_db.ledger_mark_complete(image_path)
            if self.cache_size_manager:
                try:
                    self.cache_size_manager.record_cache_write(os.path.getsize(thumbnail_path))  # disk-io: cache size tracking
                except OSError:
                    pass
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
        self.render_manager.notify(notification)

        return thumbnail_path

    def _generate_view_image_task(self, image_path: str,
                                    cancel_event: Optional[threading.Event] = None):
        """Worker task (Stage C): generates the full-resolution view image.

        Aborts if *cancel_event* is set.  Speculative tasks (those with a
        *cancel_event*) are throttled to one concurrent worker via semaphore.
        """
        is_speculative = cancel_event is not None
        if is_speculative:
            # Poll cancel_event while waiting for the semaphore so we abort
            # promptly if a FULLRES_REQUEST cancels this speculative task.
            while not self._speculative_view_sem.acquire(timeout=0.25):
                if cancel_event.is_set():
                    return None

        try:
            return self._generate_view_image_task_inner(image_path, cancel_event)
        finally:
            if is_speculative:
                self._speculative_view_sem.release()

    def _generate_view_image_task_inner(self, image_path: str,
                                         cancel_event: Optional[threading.Event] = None):
        if not self.source_cache.exists(image_path):
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

        # Re-check: view image may already exist (disk or memory cache).
        if self._mem_cache_get(image_path) is not None:
            return "memory"
        current_paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = current_paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
            logger.debug(f"View image for {image_path} already exists. Skipping.")
            return existing_view

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
        result = self._process_view_image_task(image_path, md5_hash, cancel_event=cancel_event)
        if not result:
            logger.error(f"View image generation failed for {image_path}.")
            return None

        # Send final notification with both paths now available.
        thumbnail_path = self.metadata_db.get_thumbnail_paths(image_path).get('thumbnail_path')
        is_mem_cached = (result == "memory")
        notification_data = protocol.PreviewsReadyData(
            image_entry=protocol.ImageEntryModel(path=image_path),
            thumbnail_path=thumbnail_path,
            view_image_path=None if is_mem_cached else result,
            view_image_source="memory" if is_mem_cached else "disk",
        )
        notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
        self.render_manager.notify(notification)

        return result

    def request_thumbnail(self, image_path: str, priority: Priority) -> bool:
        """Asynchronously request a thumbnail generation using the RenderManager.
        This method is now primarily for upgrading task priorities, not creating them.
        The actual task creation is handled once by the 'gui_scan_tasks' SourceJob.
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
            self.render_manager.notify(notification)
            return True

        # Slow path: check whether the task already exists in the graph.
        # If it does, upgrade its priority. If not (the background scanner hasn't
        # reached this file yet), create it immediately at the requested priority
        # so the GUI doesn't stall waiting for the generator to arrive in order.
        task_id = image_path
        with self.render_manager.graph_lock:
            task_exists = task_id in self.render_manager.task_graph

        if task_exists:
            tasks_to_upgrade = {f"meta::{image_path}", task_id}
            self.render_manager.update_task_priorities(tasks_to_upgrade, priority)
            logger.debug(f"ThumbnailManager: Upgraded priority to {priority.name} for: {image_path}")
        else:
            # Task hasn't been created by the background scanner yet.
            # Submit tasks directly — the task functions themselves
            # re-check validity when executed by a worker thread.
            self.render_manager.submit_task(
                image_path, priority, self._generate_thumbnail_task, image_path,
            )
            self.render_manager.submit_task(
                f"meta::{image_path}", priority, self._process_metadata_task, image_path
            )
            logger.debug(f"ThumbnailManager: Submitted on-demand tasks at {priority.name} for: {image_path}")
        return True

    def batch_request_thumbnails(self, image_paths: List[str], priority: Priority) -> int:
        """Batch version of request_thumbnail.  Checks thumbnail validity for all
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
            self.render_manager.notify(notification)

        # For uncached paths, check task graph in a single lock scope.
        tasks_to_upgrade = set()
        paths_to_submit = []
        with self.render_manager.graph_lock:
            for path in uncached_paths:
                if path in self.render_manager.task_graph:
                    tasks_to_upgrade.add(path)
                    tasks_to_upgrade.add(f"meta::{path}")
                else:
                    paths_to_submit.append(path)

        # Batch-upgrade existing tasks (single call, single lock acquisition).
        if tasks_to_upgrade:
            self.render_manager.update_task_priorities(tasks_to_upgrade, priority)

        # Submit new tasks for paths not yet in the graph.
        for path in paths_to_submit:
            self.render_manager.submit_task(
                path, priority, self._generate_thumbnail_task, path,
            )
            self.render_manager.submit_task(
                f"meta::{path}", priority, self._process_metadata_task, path
            )

        return len(cached_paths) + len(uncached_paths)

    # ── ComfyUI generation ──────────────────────────────────────

    def submit_comfyui_generation(self, image_path: str, prompt: str,
                                   denoise: float, workflow: str = "") -> str:
        """Submit a ComfyUI generation task. Returns the task_id."""
        if not hasattr(self, '_comfyui_client') or self._comfyui_client is None:
            from core.comfyui_client import ComfyUIClient
            host = self.config_manager.get("comfyui.host", "192.168.50.4")
            port = self.config_manager.get("comfyui.port", 8188)
            self._comfyui_client = ComfyUIClient(host, port)

        task_id = f"comfyui::{image_path}"
        self.render_manager.submit_task(
            task_id,
            Priority.NORMAL,
            self._run_comfyui_generation,
            image_path, prompt, denoise, workflow,
        )
        return task_id

    def _run_comfyui_generation(self, image_path: str, prompt: str,
                                 denoise: float, workflow: str = "",
                                 cancel_event=None):
        result_path = self._comfyui_client.generate(
            image_path, prompt, denoise, cancel_event,
            workflow_json=workflow,
        )

        if result_path:
            # Submit thumbnail + metadata tasks for the new file so it
            # appears with a proper thumbnail once the GUI adds it.
            try:
                tasks = self.create_tasks_for_file(result_path, Priority.HIGH)
                for task in tasks:
                    self.render_manager.submit_task(
                        task.task_id, task.priority, task.func, *task.args,
                        dependencies=task.dependencies, task_type=task.task_type,
                        on_complete_callback=task.on_complete_callback, **task.kwargs
                    )
            except Exception as e:  # why: ComfyUI result may reference a format with no plugin; must not abort the notification path
                logger.error("Failed to create tasks for ComfyUI result %s: %s", result_path, e)

            # Notify the GUI to add the new file to the thumbnail grid.
            scan_notification = protocol.Notification(
                type="scan_progress",
                data=protocol.ScanProgressData(
                    path=os.path.dirname(result_path),
                    files=[protocol.ImageEntryModel(path=result_path)],
                ).model_dump(),
            )
            self.render_manager.notify(scan_notification)

        notification_data = protocol.ComfyUICompleteData(
            source_path=image_path,
            result_path=result_path or "",
            status="success" if result_path else "error",
            error="" if result_path else "Generation failed",
        )
        notification = protocol.Notification(
            type="comfyui_complete",
            data=notification_data.model_dump(),
        )
        self.render_manager.notify(notification)

    def request_view_image(self, image_path: str) -> Optional[str]:
        """Requests view image generation at FULLRES_REQUEST priority.

        - If the view image is in the mem cache: returns ``"memory"`` sentinel.
        - If the view image is already on disk: returns its path immediately (no task).
        - If a view image task is in the graph: upgrades it to FULLRES_REQUEST.
        - If no task exists yet: submits _generate_view_image_task at FULLRES_REQUEST.

        Returns ``"memory"`` (mem-cached), a disk path, or None (generation queued).
        """
        if not image_path:
            return None

        # Fast path: view image in daemon memory cache.
        if self._mem_cache_get(image_path) is not None:
            return "memory"

        # Fast path: view image already cached on disk.
        paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
            return existing_view

        # Fast path: natively viewable format with no rotation needed.
        _, ext = os.path.splitext(image_path)
        if ext.lower() in _NATIVELY_VIEWABLE:
            meta = self.metadata_db.get_metadata(image_path)
            if meta and meta.get('orientation') == 1:
                return "direct:" + image_path

        view_task_id = f"view::{image_path}"

        with self.render_manager.graph_lock:
            task_exists = view_task_id in self.render_manager.task_graph

        if task_exists:
            self.render_manager.update_task_priorities(
                {view_task_id}, Priority.FULLRES_REQUEST
            )
            logger.debug(f"ThumbnailManager: Upgraded view image task to FULLRES_REQUEST for: {image_path}")
        else:
            self.render_manager.submit_task(
                view_task_id, Priority.FULLRES_REQUEST,
                self._generate_view_image_task, image_path,
            )
            logger.debug(f"ThumbnailManager: Submitted FULLRES_REQUEST view image task for: {image_path}")

        # Cancel speculative view tasks to free workers for this request.
        # Speculative tasks have a cancel_event; direct requests do not.
        with self.render_manager.graph_lock:
            speculative_to_cancel = [
                tid for tid, t in self.render_manager.task_graph.items()
                if tid.startswith("view::") and tid != view_task_id and t.cancel_event
            ]
        if speculative_to_cancel:
            self.render_manager.cancel_tasks(speculative_to_cancel)
            logger.debug("Cancelled %d speculative view tasks for FULLRES_REQUEST", len(speculative_to_cancel))

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

    def request_speculative_fullres(self, image_path: str, priority: Priority):
        """Submit or upgrade a speculative fullres task for heatmap pre-caching."""
        if self.cache_size_manager and self.cache_size_manager.is_cache_full():
            return

        view_task_id = f"view::{image_path}"

        paths = self.metadata_db.get_thumbnail_paths(image_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
            return

        # Only create a new Event if the task doesn't already exist;
        # submit_task preserves the existing cancel_event on upgrade.
        with self.render_manager.graph_lock:
            existing = self.render_manager.task_graph.get(view_task_id)
        evt = existing.cancel_event if existing else threading.Event()

        self.render_manager.submit_task(
            view_task_id, priority,
            self._generate_view_image_task, image_path,
            cancel_event=evt,
        )

    def cancel_speculative_fullres(self, image_path: str):
        self.render_manager.cancel_task(f"view::{image_path}")

    def cancel_speculative_fullres_batch(self, image_paths: List[str]):
        self.render_manager.cancel_tasks([f"view::{p}" for p in image_paths])

    def _process_view_image_task(self, image_path: str, md5_hash: str,
                                   cancel_event: Optional[threading.Event] = None):
        logger.debug(f"Starting view image task for {image_path}")
        if not self.source_cache.exists(image_path):
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
        if current_view_image_path and os.path.exists(current_view_image_path):  # disk-io: cache file check
            logger.debug(f"View image for {image_path} already exists at {current_view_image_path}. Skipping generation.")
            return current_view_image_path

        if cancel_event and cancel_event.is_set():
            return None

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for format: {ext}")
            return None

        start_time = time.time()
        view_image_path = plugin.process_view_image(image_path, md5_hash)
        duration = time.time() - start_time
        logger.debug(f"plugin.process_view_image for {os.path.basename(image_path)} took {duration:.4f} seconds.")
        if not view_image_path:
            return None

        if duration < self._fullres_cache_threshold:
            # Fast extraction → RAM only, delete disk file.
            try:
                with open(view_image_path, 'rb') as f:  # disk-io: mem-cache read
                    image_bytes = f.read()
                os.remove(view_image_path)
            except OSError:
                # Disk file vanished — fall through to disk-cache path.
                logger.warning("Failed to read/remove fast view image for mem cache: %s", view_image_path)
                return view_image_path
            self._mem_cache_put(image_path, image_bytes)
            logger.debug("View image for %s stored in mem cache (%d bytes, %.1fms)",
                         os.path.basename(image_path), len(image_bytes), duration * 1000)
            return "memory"

        # Slow extraction → persist to disk as before.
        self.metadata_db.set_thumbnail_paths(image_path, view_image_path=view_image_path)
        if self.cache_size_manager:
            try:
                self.cache_size_manager.record_cache_write(os.path.getsize(view_image_path))  # disk-io: cache size tracking
            except OSError:
                pass
        return view_image_path

    def _process_metadata_task(self, image_path: str):
        """Fast metadata scan (orientation, rating, file_size).
        Queues a deferred full exiftool extraction at BACKGROUND_SCAN."""
        logger.debug(f"Starting fast metadata extraction for {image_path}")
        if not self.source_cache.exists(image_path):
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
        if not self.source_cache.exists(image_path):
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

    def _resolve_write_mode(self, ext: str) -> str:
        """Return 'sidecar' or 'embedded' for the given file extension."""
        overrides = self.config_manager.get("metadata.format_write_mode", {})
        if ext in overrides:
            return overrides[ext]
        return self.config_manager.get("metadata.default_write_mode", "sidecar")

    def write_rating_to_file(self, file_path: str, rating: int):
        """Writes the rating via sidecar or embedded XMP based on config.

        Returns True on success, False on failure.
        """
        from plugins.base_plugin import sidecar_path_for

        if not os.path.exists(file_path):  # disk-io: write guard
            logger.warning(f"File not found, cannot write rating: {file_path}")
            return False

        ext = os.path.splitext(file_path)[1].lower()
        mode = self._resolve_write_mode(ext)

        if self.watchdog_handler:
            suppress_path = file_path if mode == "embedded" else sidecar_path_for(file_path)
            self.watchdog_handler.ignore_next_modification(suppress_path)

        plugin = self.plugin_registry.get_plugin_for_format(ext)

        if plugin and plugin.is_available():
            if mode == "embedded":
                success = plugin.write_rating_embedded(file_path, rating)
            else:
                success = plugin.write_rating(file_path, rating)
            if success:
                self.metadata_db.pending_write_remove(
                    file_path, 'rating', {'rating': rating})
            else:
                logger.error(f"Plugin failed to write rating for {file_path}")
            return success

        logger.warning(f"No plugin found or available for format {ext} to write rating for {file_path}")
        return False

    def write_tags_to_file(self, file_path: str, tag_names: list):
        """Writes the full tag list via sidecar or embedded XMP based on config.

        Mirrors write_rating_to_file: watchdog suppression, plugin lookup, exiftool write.
        """
        from plugins.base_plugin import sidecar_path_for

        if not os.path.exists(file_path):  # disk-io: write guard
            logger.warning(f"File not found, cannot write tags: {file_path}")
            return False

        ext = os.path.splitext(file_path)[1].lower()
        mode = self._resolve_write_mode(ext)

        if self.watchdog_handler:
            suppress_path = file_path if mode == "embedded" else sidecar_path_for(file_path)
            self.watchdog_handler.ignore_next_modification(suppress_path)

        plugin = self.plugin_registry.get_plugin_for_format(ext)

        if plugin and plugin.is_available():
            if mode == "embedded":
                success = plugin.write_tags_embedded(file_path, tag_names)
            else:
                success = plugin.write_tags(file_path, tag_names)
            if success:
                self.metadata_db.pending_write_remove(
                    file_path, 'tags', {'tags': tag_names})
            else:
                logger.error(f"Plugin failed to write tags for {file_path}")
            return success

        logger.warning(f"No plugin found or available for format {ext} to write tags for {file_path}")
        return False

    def write_orientation_to_file(self, file_path: str, orientation: int) -> bool:
        from plugins.base_plugin import sidecar_path_for

        if not os.path.exists(file_path):  # disk-io: write guard
            logger.warning(f"File not found, cannot write orientation: {file_path}")
            return False

        ext = os.path.splitext(file_path)[1].lower()
        mode = self._resolve_write_mode(ext)

        if self.watchdog_handler:
            suppress_path = file_path if mode == "embedded" else sidecar_path_for(file_path)
            self.watchdog_handler.ignore_next_modification(suppress_path)

        plugin = self.plugin_registry.get_plugin_for_format(ext)
        if plugin and plugin.is_available():
            if mode == "embedded":
                success = plugin.write_orientation_embedded(file_path, orientation)
            else:
                success = plugin.write_orientation(file_path, orientation)
            if success:
                self.metadata_db.pending_write_remove(
                    file_path, 'orientation', {'orientation': orientation})
            return success

        logger.warning(f"No plugin found for format {ext} to write orientation for {file_path}")
        return False

    def invalidate_cached_images(self, file_path: str):
        """Deletes cached thumbnail, view image, and mem-cache entry for regeneration."""
        paths = self.metadata_db.get_thumbnail_paths(file_path)
        for key in ('thumbnail_path', 'view_image_path'):
            cached = paths.get(key)
            if cached and os.path.exists(cached):  # disk-io: cache cleanup
                try:
                    os.remove(cached)
                    logger.debug("Removed cached %s: %s", key, cached)
                except OSError as e:
                    logger.warning("Failed to remove cached %s: %s", cached, e)
        self.invalidate_mem_cache(file_path)
        self.metadata_db.clear_thumbnail_paths(file_path)

    def get_cached_thumbnail_path(self, md5_hash: str) -> str:
        return os.path.join(self.thumbnail_cache_dir, f"{md5_hash}.jpg")
    
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
                os.stat(mount_point)  # disk-io: volume accessibility probe
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
        start_time = time.time()
        try:
            with open(file_path, "rb") as f:  # disk-io: prefetch header read
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

    def request_metadata_extraction(self, image_paths: List[str], priority: Priority = Priority.NORMAL):
        logger.info(f"Queueing metadata extraction for {len(image_paths)} images with {priority.name} priority.")
        for image_path in image_paths:
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
                self.render_manager.notify(notification)
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
        Returns an empty list if the view image already exists (on disk or in
        memory cache) or the file is not a supported format.
        """
        if not self._passes_pre_checks(file_path):
            return []

        # Mem-cached view images have no DB view_image_path; skip to avoid no-op tasks.
        if self._mem_cache_get(file_path) is not None:
            return []

        paths = self.metadata_db.get_thumbnail_paths(file_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
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

        # Mem-cached view images have no DB view_image_path; skip to avoid no-op tasks.
        if self._mem_cache_get(file_path) is None:
            paths = self.metadata_db.get_thumbnail_paths(file_path)
            existing_view = paths.get('view_image_path') if paths else None
            if not (existing_view and os.path.exists(existing_view)):  # disk-io: cache file check
                tasks.append(RenderTask(
                    task_id=f"view::{file_path}",
                    priority=priority,
                    func=self._generate_view_image_task,
                    args=(file_path,),
                ))

        if not tasks:
            # Everything already valid — clear from ledger so this file
            # is not re-submitted as an orphan on every daemon restart.
            self.metadata_db.ledger_mark_complete(file_path)

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
        # Mem-cached view images have no DB view_image_path; skip to avoid no-op tasks.
        if self._mem_cache_get(file_path) is None:
            paths = self.metadata_db.get_thumbnail_paths(file_path)
            existing_view = paths.get('view_image_path') if paths else None
            if not (existing_view and os.path.exists(existing_view)):  # disk-io: cache file check
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
        self.metadata_db.close()
        logger.info("ThumbnailManager: Shutdown complete.")

    # ------------------------------------------------------------------
    #  Pending-write recovery
    # ------------------------------------------------------------------

    def recover_pending_writes(self) -> int:
        pending = self.metadata_db.pending_write_get_all()
        if not pending:
            return 0

        count = 0
        for row in pending:
            fp = row['file_path']
            wt = row['write_type']
            payload = row['payload']

            if wt == 'rating':
                self.render_manager.submit_task(
                    f"write_rating::{fp}", Priority.NORMAL,
                    self.write_rating_to_file, fp, payload['rating'],
                    task_type=TaskType.SIMPLE,
                )
            elif wt == 'orientation':
                self.render_manager.submit_task(
                    f"write_orientation::{fp}", Priority.NORMAL,
                    self.write_orientation_to_file, fp, payload['orientation'],
                    task_type=TaskType.SIMPLE,
                )
            elif wt == 'tags':
                self.render_manager.submit_task(
                    f"write_tags::{fp}", Priority.NORMAL,
                    self.write_tags_to_file, fp, payload['tags'],
                    task_type=TaskType.SIMPLE,
                )
            else:
                logger.warning(f"Unknown pending write type: {wt} for {fp}")
                continue
            count += 1

        logger.info(f"Recovered {count} pending file writes from prior session")
        return count

