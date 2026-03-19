
import os
import shutil
import stat as stat_mod
import time
import logging
import fnmatch
import threading
from typing import Optional, List, Set
from core.metadata_database import MetadataDatabase
from core.priority import NATIVELY_VIEWABLE
from core.rendermanager import Priority, RenderManager, RenderTask
from core.source_cache import SourceExistsCache
from core.ai_task_coordinator import AITaskCoordinator
from core.phash_coordinator import PHashCoordinator
from core.metadata_writer import MetadataWriter
from core.volume_prober import VolumeProber
from plugins.base_plugin import plugin_registry
from plugins.exiftool_process import shutdown_all as _shutdown_exiftool_processes
from core.fullres_mem_cache import FullresMemCache
from core.task_operations import TaskOperationRegistry
from core import notifications as protocol

logger = logging.getLogger(__name__)

# Bump when cached image generation changes in a way that invalidates
# existing thumbnails/view-images (e.g., orientation handling).
CACHE_VERSION = 2


class ThumbnailManager:
    def __init__(self, config_manager, metadata_database: MetadataDatabase, watchdog_handler=None, event_system=None, num_workers=None):
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

        if num_workers is None:
            num_workers = config_manager.get("num_workers", max(2, (os.cpu_count() or 4) - 1))
        self.render_manager = RenderManager(num_workers=num_workers)
        self.render_manager.start()
        self._watchdog_handler = watchdog_handler

        self.volume_prober = VolumeProber(config_manager)
        self.metadata_writer = MetadataWriter(
            config_manager, self.plugin_registry, metadata_database,
            self.render_manager, watchdog_handler=watchdog_handler)
        self.ai_coordinator = AITaskCoordinator(
            config_manager, metadata_database, self.render_manager)
        self.phash_coordinator = PHashCoordinator(metadata_database, self.render_manager)
        self.cache_size_manager = None  # set by daemon after construction
        self.source_cache = SourceExistsCache(ttl=30.0)

        # In-memory LRU cache for fast fullres extractions (below threshold → RAM only).
        max_mb = config_manager.get("fullres_mem_cache_mb", 512)
        self.fullres_cache = FullresMemCache(max_bytes=max_mb * 1024 * 1024)
        threshold_ms = config_manager.get("fullres_cache_threshold_ms", 500)
        self._fullres_cache_threshold = threshold_ms / 1000.0

        self.task_ops = TaskOperationRegistry(metadata_database, event_system=event_system)

        # Limit speculative view tasks to 1 concurrent worker so user
        # requests at FULLRES_REQUEST are never starved.
        self._speculative_view_sem = threading.Semaphore(1)

    @property
    def watchdog_handler(self):
        return self._watchdog_handler

    @watchdog_handler.setter
    def watchdog_handler(self, handler):
        self._watchdog_handler = handler
        self.metadata_writer.watchdog_handler = handler

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

        rows = self.metadata_db.images.clear_all_thumbnail_paths()
        logger.info("Cache migration: cleared %d DB thumbnail/view_image paths.", rows)

        # Reset work-intent ledger and scan ledger so the daemon re-walks
        # all directories and re-generates thumbnails/view images.
        self.metadata_db.ledgers.file_work_clear_all()
        self.metadata_db.ledgers.ledger_reset_all()
        logger.info("Cache migration: cleared file_work and scan_ledger tables.")

        import shutil
        for d in (self.thumbnail_cache_dir, self.image_cache_dir):
            if os.path.isdir(d):  # disk-io: cache dir cleanup
                shutil.rmtree(d)
                os.makedirs(d, exist_ok=True)
        logger.info("Cache migration: deleted and recreated thumbnail/image cache directories.")

        with open(version_file, "w") as f:  # disk-io: cache version marker
            f.write(str(CACHE_VERSION))
        logger.info("Cache migration complete. Version marker set to %d.", CACHE_VERSION)

    # -----------------------------------------------------------------------

    def load_plugins(self) -> None:
        plugin_registry.load_plugins_from_directory(self._plugins_dir, self.cache_dir, self.thumbnail_size)
        self.supported_formats = self.plugin_registry.get_supported_formats()
        if not self.supported_formats:
            logger.warning("No format plugins loaded — scanning and thumbnailing will be non-functional.")
        else:
            logger.info(f"ThumbnailManager supports {len(self.supported_formats)} formats: {sorted(self.supported_formats)}")

    def get_thumbnail(self, image_path):
        """Blocks until a thumbnail is available; use request_thumbnail for grid loading."""
        if not os.path.exists(image_path):  # disk-io: source existence guard
            logger.error(f"ThumbnailManager: Image not found: {image_path}")
            return None

        # Check if thumbnail is already valid in DB and exists on disk
        valid, paths = self.metadata_db.images.check_thumbnail_validity(image_path)
        if valid:
            thumbnail_path = paths.get('thumbnail_path')
            if thumbnail_path:
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
        header_result = self.volume_prober.read_file_header(image_path)
        if not header_result:
            return None
        md5_hash, prefetch_buffer = header_result

        thumbnail_path = plugin.process_thumbnail(image_path, md5_hash, prefetch_buffer=prefetch_buffer)

        if thumbnail_path:
            self.metadata_db.images.set_thumbnail_paths(image_path, thumbnail_path=thumbnail_path)
            self.metadata_db.images.set_content_hash(image_path, md5_hash)
            self.phash_coordinator.compute_and_store(image_path, thumbnail_path)
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
        """Uses ``source_cache.stat()`` so files recently stat'd by the directory
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
            self.metadata_db.ledgers.file_work_remove(image_path, 'thumbnail')
            raise FileNotFoundError(f"Original file not found, record will be cleaned up: {image_path}")

        if not self.volume_prober.is_accessible(image_path):
            return None

        # Re-check validity — another task may have already processed this file.
        st = self.source_cache.stat(image_path)
        valid, paths = self.metadata_db.images.check_thumbnail_validity(image_path, stat_result=st)
        if valid:
            logger.debug(f"Thumbnail for {image_path} already valid. Sending notification and skipping.")
            self.metadata_db.ledgers.ledger_mark_complete(image_path)
            self.metadata_db.ledgers.file_work_remove(image_path, 'thumbnail')
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
            self.metadata_db.ledgers.file_work_remove(image_path, 'thumbnail')
            return None

        header_result = self.volume_prober.read_file_header(image_path)
        if not header_result:
            return None
        md5_hash, prefetch_buffer = header_result

        # Pass the already-read header buffer so plugins can avoid a second NAS
        # read for orientation and thumbnail extraction.
        thumbnail_path = plugin.process_thumbnail(image_path, md5_hash, prefetch_buffer=prefetch_buffer)
        if thumbnail_path:
            self.metadata_db.images.set_thumbnail_paths(image_path, thumbnail_path=thumbnail_path, stat_result=st)
            self.metadata_db.images.set_content_hash(image_path, md5_hash)
            self.phash_coordinator.compute_and_store(image_path, thumbnail_path)
            self.metadata_db.ledgers.ledger_mark_complete(image_path)
            self.metadata_db.ledgers.file_work_remove(image_path, 'thumbnail')
            if self.cache_size_manager:
                try:
                    self.cache_size_manager.record_cache_write(os.path.getsize(thumbnail_path))  # disk-io: cache size tracking
                except OSError:
                    pass
        else:
            logger.error(f"Thumbnail generation failed for {image_path}.")

        # Send notification immediately — do not wait for the view image (Stage C).
        # Include view_image_path if it already exists from a prior run.
        existing_view = self.metadata_db.images.get_thumbnail_paths(image_path).get('view_image_path')
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
            self.metadata_db.ledgers.file_work_remove(image_path, 'view_image')
            raise FileNotFoundError(f"Original file not found: {image_path}")

        if not self.volume_prober.is_accessible(image_path):
            return None

        # Re-check: view image may already exist (disk or memory cache).
        if self.fullres_cache.get(image_path) is not None:
            return "memory"
        current_paths = self.metadata_db.images.get_thumbnail_paths(image_path)
        existing_view = current_paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
            logger.debug(f"View image for {image_path} already exists. Skipping.")
            self.metadata_db.ledgers.file_work_remove(image_path, 'view_image')
            return existing_view

        _, ext = os.path.splitext(image_path)
        plugin = self.plugin_registry.get_plugin_for_format(ext.lower())
        if not plugin:
            logger.error(f"ThumbnailManager: No plugin found for: {image_path}")
            self.metadata_db.ledgers.file_work_remove(image_path, 'view_image')
            return None

        # PIL-native formats (JPEG, PNG, etc.) are directly displayable —
        # skip decode+re-encode and use the source bytes directly.
        from plugins.pil_plugin import PILPlugin
        if isinstance(plugin, PILPlugin):
            mount = self.volume_prober.get_mount_point(image_path)
            if mount is None:
                # Local file — point GUI at source; no copy needed.
                self.metadata_db.images.set_thumbnail_paths(image_path, view_image_path=image_path)
                result = image_path
            else:
                # NAS file — copy to local cache (raw copy, no decode).
                if cancel_event and cancel_event.is_set():
                    return None
                header_result = self.volume_prober.read_file_header(image_path)
                if not header_result:
                    return None
                md5_hash, _ = header_result
                view_image_path = plugin.get_view_image_path(md5_hash)
                try:
                    os.makedirs(os.path.dirname(view_image_path), exist_ok=True)
                    shutil.copy2(image_path, view_image_path)  # disk-io: NAS full-file copy
                except OSError as e:
                    logger.warning("Failed to copy NAS file to cache: %s: %s", image_path, e)
                    return None
                self.metadata_db.images.set_thumbnail_paths(image_path, view_image_path=view_image_path)
                if self.cache_size_manager:
                    try:
                        self.cache_size_manager.record_cache_write(os.path.getsize(view_image_path))  # disk-io: cache size tracking
                    except OSError:
                        pass
                result = view_image_path
        else:
            header_result = self.volume_prober.read_file_header(image_path)
            if not header_result:
                return None
            md5_hash, prefetch_buffer = header_result

            if cancel_event and cancel_event.is_set():
                return None

            from plugins.exiftool_process import ExifToolCancelled
            try:
                result = self._process_view_image_task(image_path, md5_hash,
                                                       cancel_event=cancel_event,
                                                       prefetch_buffer=prefetch_buffer)
            except ExifToolCancelled:
                logger.debug("View image cancelled for %s", os.path.basename(image_path))
                return None
            if not result:
                logger.error(f"View image generation failed for {image_path}.")
                return None

        # Send final notification with both paths now available.
        thumbnail_path = self.metadata_db.images.get_thumbnail_paths(image_path).get('thumbnail_path')
        is_mem_cached = (result == "memory")
        notification_data = protocol.PreviewsReadyData(
            image_entry=protocol.ImageEntryModel(path=image_path),
            thumbnail_path=thumbnail_path,
            view_image_path=None if is_mem_cached else result,
            view_image_source="memory" if is_mem_cached else "disk",
        )
        notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
        self.render_manager.notify(notification)
        self.metadata_db.ledgers.file_work_remove(image_path, 'view_image')

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
        cached = self.metadata_db.images.get_cached_thumbnail_paths(image_path)
        if cached and cached.get('thumbnail_path'):
            logger.debug("cache hit: %s", image_path)
            notification_data = protocol.PreviewsReadyData(
                image_entry=protocol.ImageEntryModel(path=image_path),
                thumbnail_path=cached['thumbnail_path'],
                view_image_path=cached.get('view_image_path')
            )
            notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
            self.render_manager.notify(notification)
            return True

        logger.debug("cache miss: %s", image_path)
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
        validity = self.metadata_db.images.batch_get_cached_thumbnail_validity(image_paths)

        # Separate cached (valid) from uncached paths.
        cached_paths = []
        uncached_paths = []
        for path in image_paths:
            info = validity.get(path)
            if info and info['valid']:
                cached_paths.append((path, info))
            else:
                uncached_paths.append(path)

        if cached_paths:
            logger.debug("batch cache hit: %d/%d paths", len(cached_paths), len(image_paths))
        if uncached_paths:
            logger.debug("batch cache miss: %d/%d paths", len(uncached_paths), len(image_paths))

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

    # -- AI task delegation (see core/ai_task_coordinator.py) -----------------

    def submit_clip_indexing_job(self, directory: str, file_paths: List[str]):
        self.ai_coordinator.submit_clip_indexing_job(directory, file_paths)

    def submit_auto_orient_job(self, directory: str, file_paths: List[str]):
        self.ai_coordinator.submit_auto_orient_job(directory, file_paths)

    def submit_face_detection_job(self, directory: str, file_paths: List[str]):
        self.ai_coordinator.submit_face_detection_job(directory, file_paths)

    def submit_phash_job(self, directory: str, file_paths: List[str],
                         priority: Priority = Priority.BACKGROUND_SCAN):
        self.phash_coordinator.submit_phash_job(directory, file_paths, priority)

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
        if self.fullres_cache.get(image_path) is not None:
            return "memory"

        # Fast path: view image already cached on disk.
        paths = self.metadata_db.images.get_thumbnail_paths(image_path)
        existing_view = paths.get('view_image_path')
        if existing_view and os.path.exists(existing_view):  # disk-io: cache file check
            return existing_view

        # Fast path: natively viewable format with no rotation needed.
        _, ext = os.path.splitext(image_path)
        if ext.lower() in NATIVELY_VIEWABLE:
            meta = self.metadata_db.images.get_metadata(image_path)
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

        paths = self.metadata_db.images.get_thumbnail_paths(image_path)
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
                                   cancel_event: Optional[threading.Event] = None,
                                   prefetch_buffer: Optional[bytes] = None):
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

        current_paths = self.metadata_db.images.get_thumbnail_paths(image_path)
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
        view_image_path = plugin.process_view_image(image_path, md5_hash,
                                                    cancel_event=cancel_event,
                                                    prefetch_buffer=prefetch_buffer)
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
            self.fullres_cache.put(image_path, image_bytes)
            logger.debug("View image for %s stored in mem cache (%d bytes, %.1fms)",
                         os.path.basename(image_path), len(image_bytes), duration * 1000)
            return "memory"

        # Slow extraction → persist to disk as before.
        self.metadata_db.images.set_thumbnail_paths(image_path, view_image_path=view_image_path)
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

        if not self.volume_prober.is_accessible(image_path):
            return

        start_time = time.time()
        st = self.source_cache.stat(image_path)
        self.metadata_db.extract_and_store_fast_metadata(image_path, stat_result=st)
        duration = time.time() - start_time
        logger.debug(f"Fast metadata for {os.path.basename(image_path)} took {duration:.4f}s")
        self.metadata_db.ledgers.file_work_remove(image_path, 'metadata')

        if self.metadata_db.images.needs_full_metadata(image_path):
            self.render_manager.submit_task(
                f"meta_full::{image_path}",
                Priority.BACKGROUND_SCAN,
                self._process_full_metadata_task,
                image_path,
            )

    def _process_full_metadata_task(self, image_path: str):
        if not self.source_cache.exists(image_path):
            return
        if not self.volume_prober.is_accessible(image_path):
            return
        # Re-check: another worker may have completed this between scheduling and execution
        if not self.metadata_db.images.needs_full_metadata(image_path):
            return

        start_time = time.time()
        self.metadata_db.extract_and_store_full_metadata(image_path)
        duration = time.time() - start_time
        logger.debug(f"Full metadata for {os.path.basename(image_path)} took {duration:.4f}s")

    # -- Write-back delegation (see core/metadata_writer.py) ------------------

    def write_rating_to_file(self, file_path: str, rating: int) -> bool:
        return self.metadata_writer.write_rating(file_path, rating)

    def write_tags_to_file(self, file_path: str, tag_names: list) -> bool:
        return self.metadata_writer.write_tags(file_path, tag_names)

    def write_orientation_to_file(self, file_path: str, orientation: int) -> bool:
        return self.metadata_writer.write_orientation(file_path, orientation)

    def invalidate_cached_images(self, file_path: str):
        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        for key in ('thumbnail_path', 'view_image_path'):
            cached = paths.get(key)
            if cached and os.path.exists(cached):  # disk-io: cache cleanup
                try:
                    os.remove(cached)
                    logger.debug("Removed cached %s: %s", key, cached)
                except OSError as e:
                    logger.warning("Failed to remove cached %s: %s", cached, e)
        self.fullres_cache.invalidate(file_path)
        self.metadata_db.images.clear_thumbnail_paths(file_path)

    def get_cached_thumbnail_path(self, md5_hash: str) -> str:
        return os.path.join(self.thumbnail_cache_dir, f"{md5_hash}.jpg")
    
    def is_format_supported(self, image_path: str) -> bool:
        _, ext = os.path.splitext(image_path)
        return self.plugin_registry.get_plugin_for_format(ext.lower()) is not None
    
    def get_supported_formats(self) -> List[str]:
        return list(self.supported_formats)

    def request_metadata_extraction(self, image_paths: List[str], priority: Priority = Priority.NORMAL):
        logger.info(f"Queueing metadata extraction for {len(image_paths)} images with {priority.name} priority.")
        for image_path in image_paths:
            self.render_manager.submit_task(
                f"meta::{image_path}",
                priority,
                self._process_metadata_task,
                image_path,
            )

    def _check_file_readiness(self, file_path: str):
        """Shared pre-flight for task factories.

        Returns ``(thumb_valid, paths)`` after running ``_passes_pre_checks``
        and a single ``check_thumbnail_validity`` call, or ``None`` if the
        file should be skipped entirely.
        """
        if not self._passes_pre_checks(file_path):
            return None
        st = self.source_cache.stat(file_path)
        return self.metadata_db.images.check_thumbnail_validity(file_path, stat_result=st)

    def _needs_view_image(self, file_path: str, paths: dict) -> bool:
        if self.fullres_cache.get(file_path) is not None:
            return False
        existing_view = paths.get('view_image_path')
        return not (existing_view and os.path.exists(existing_view))  # disk-io: cache file check

    def _make_thumb_meta_tasks(self, file_path: str, priority: Priority) -> List[RenderTask]:
        return [
            RenderTask(
                task_id=f"meta::{file_path}",
                priority=priority,
                func=self._process_metadata_task,
                args=(file_path,),
            ),
            RenderTask(
                task_id=file_path,
                priority=priority,
                func=self._generate_thumbnail_task,
                args=(file_path,),
            ),
        ]

    def _make_view_task(self, file_path: str, priority: Priority) -> RenderTask:
        return RenderTask(
            task_id=f"view::{file_path}",
            priority=priority,
            func=self._generate_view_image_task,
            args=(file_path,),
        )

    def create_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        readiness = self._check_file_readiness(file_path)
        if readiness is None:
            return []

        valid, paths = readiness
        if valid:
            logger.debug(f"Previews for {file_path} already valid. No tasks created.")
            if priority >= Priority.GUI_REQUEST_LOW:
                notification_data = protocol.PreviewsReadyData(
                    image_entry=protocol.ImageEntryModel(path=file_path),
                    thumbnail_path=paths.get('thumbnail_path'),
                    view_image_path=paths.get('view_image_path')
                )
                notification = protocol.Notification(type="previews_ready", data=notification_data.model_dump())
                self.render_manager.notify(notification)
            return []

        return self._make_thumb_meta_tasks(file_path, priority)

    def create_view_image_task_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        if not self._passes_pre_checks(file_path):
            return []

        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        if not self._needs_view_image(file_path, paths):
            return []

        return [self._make_view_task(file_path, priority)]

    def create_all_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """Task factory for daemon background indexing: creates thumbnail, metadata,
        and view image tasks in a single pass."""
        readiness = self._check_file_readiness(file_path)
        if readiness is None:
            return []

        tasks: List[RenderTask] = []
        valid, paths = readiness

        if not valid:
            tasks.extend(self._make_thumb_meta_tasks(file_path, priority))

        if self._needs_view_image(file_path, paths):
            tasks.append(self._make_view_task(file_path, priority))

        if not tasks:
            # Everything already valid — clear from ledger so this file
            # is not re-submitted as an orphan on every daemon restart.
            self.metadata_db.ledgers.ledger_mark_complete(file_path)
            for wt in ('thumbnail', 'view_image', 'metadata'):
                self.metadata_db.ledgers.file_work_remove(file_path, wt)

        return tasks

    def create_gui_tasks_for_file(self, file_path: str, priority: Priority) -> List[RenderTask]:
        """Task factory for GUI directory loads.

        Like create_all_tasks_for_file but assigns view-image tasks at
        BACKGROUND_SCAN regardless of *priority*.
        """
        readiness = self._check_file_readiness(file_path)
        if readiness is None:
            return []

        tasks: List[RenderTask] = []
        thumb_valid, paths = readiness

        if not thumb_valid:
            tasks.extend(self._make_thumb_meta_tasks(file_path, priority))

        if self._needs_view_image(file_path, paths):
            tasks.append(self._make_view_task(file_path, Priority.BACKGROUND_SCAN))

        return tasks

    def shutdown(self) -> None:
        logger.info("ThumbnailManager: Shutting down.")
        self.render_manager.shutdown()
        _shutdown_exiftool_processes()
        self.metadata_db.close()
        logger.info("ThumbnailManager: Shutdown complete.")

    def recover_pending_writes(self) -> int:
        return self.metadata_writer.recover_pending_writes()

