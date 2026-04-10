"""In-process facade for the GUI.

Delegates to ThumbnailManager / MetadataDatabase / RenderManager directly.
"""
import logging
import os
import threading
import time
from typing import List, Optional, Dict, Any, Tuple

from core.directory_scanner import DirectoryScanner, ReconcileContext
from core.event_system import event_system, EventType, StatusMessageEventData, StatusSection
from core.folder_node import FolderNode
from core.notifications import Notification, FilesRemovedData, ImageEntryModel
from core.rendermanager import Priority, TaskType, SourceJob
import numpy as np

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10


def _batch_iter(files: list):
    """Yield successive 10-item slices — shared by post-scan task generators."""
    for i in range(0, len(files), _BATCH_SIZE):
        yield files[i:i + _BATCH_SIZE]


class ThumbnailService:
    """In-process service facade used by the GUI."""

    def __init__(self, thumbnail_manager, directory_scanner: DirectoryScanner):
        self.tm = thumbnail_manager
        self.db = thumbnail_manager.metadata_db
        self.rm = thumbnail_manager.render_manager
        self.config_manager = thumbnail_manager.config_manager
        self.directory_scanner = directory_scanner
        self._compound_task_counter = 0
        self._counter_lock = threading.Lock()
        self._clip_cache = None       # (file_paths, matrix) tuple
        self._clip_cache_gen = -1     # db.embedding_generation when cache was built
        self._clip_cache_lock = threading.Lock()

        from core.ocio_manager import ocio_manager
        ocio_manager.init(self.db)

    def prepare_for_shutdown(self):
        self.rm.prepare_for_shutdown()

    # ------------------------------------------------------------------
    #  Directory / File Discovery
    # ------------------------------------------------------------------

    def get_directory_files(self, path: str, recursive: bool = True):
        """Return cached files immediately, start reconciliation scan in background.

        Returns a dict with 'files' (sorted list of paths) and
        'thumbnail_paths' (dict mapping file_path → thumbnail_path).
        """
        db_files = self.db.images.get_directory_files(path, recursive=recursive)
        logger.info(
            f"DB returned {len(db_files)} cached files for '{path}' "
            f"(recursive={recursive}). Starting reconciliation walk."
        )

        thumb_map: Dict[str, str] = {}
        if db_files:
            t0 = time.perf_counter()
            validity = self.db.images.batch_get_cached_thumbnail_validity(db_files)
            thumb_map = {
                fp: info['thumbnail_path']
                for fp, info in validity.items()
                if info.get('valid') and info.get('thumbnail_path')
            }
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                f"[startup] batch thumbnail lookup: {len(thumb_map)}/{len(db_files)} "
                f"cached in {elapsed_ms:.0f} ms"
            )

        # Start reconciliation scan
        reconcile_ctx = ReconcileContext(db_file_set=set(db_files))

        def _on_reconcile_complete():
            if reconcile_ctx.inaccessible:
                event_system.publish(StatusMessageEventData(
                    event_type=EventType.STATUS_MESSAGE,
                    source="thumbnail_service",
                    timestamp=time.time(),
                    message=f"Directory not accessible (volume unmounted?): {path}",
                    section=StatusSection.PROCESS,
                    permanent=True,
                ))
            elif reconcile_ctx.scan_had_errors:
                event_system.publish(StatusMessageEventData(
                    event_type=EventType.STATUS_MESSAGE,
                    source="thumbnail_service",
                    timestamp=time.time(),
                    message=f"Scan incomplete (filesystem error) — cached data preserved: {path}",
                    section=StatusSection.PROCESS,
                    permanent=True,
                ))
            if reconcile_ctx.ghost_files:
                logger.info(
                    f"Reconciliation found {len(reconcile_ctx.ghost_files)} "
                    f"ghost files for '{path}'."
                )
                notification = Notification(
                    type="files_removed",
                    data=FilesRemovedData(
                        files=[ImageEntryModel(path=p) for p in reconcile_ctx.ghost_files]
                    ).model_dump(),
                )
                self.rm.notify(notification)
                self.db.remove_records(reconcile_ctx.ghost_files)

            discovered = reconcile_ctx.discovered_files
            if discovered:
                logger.info(
                    f"Post-scan: creating tasks for {len(discovered)} "
                    f"discovered files in '{path}'."
                )

                discovered_list = list(discovered)
                for wt in ('thumbnail', 'view_image', 'metadata'):
                    self.db.ledgers.file_work_batch_insert(discovered_list, wt, scan_root=path)

                task_job = SourceJob(
                    job_id=f"post_scan::{path}",
                    priority=Priority.LOW,
                    task_priority=Priority.LOW,
                    generator=_batch_iter(discovered_list),
                    task_factory=self.tm.create_gui_tasks_for_file,
                    create_tasks=True,
                    suppress_progress=True,
                )
                self.rm.submit_source_job(task_job)

            self.db.ledgers.ledger_prune_complete(path)

            # Chain AI background jobs after scan completes
            all_files = list(discovered) + [f for f in db_files if f not in discovered]
            if all_files:
                cfg = self.tm.config_manager
                if cfg.get("ai.enabled", True):
                    if cfg.get("ai.auto_orient.enabled", False):
                        self.db.ledgers.file_work_batch_insert(all_files, 'auto_orient', scan_root=path)
                    # CLIP and face detection are daemon-only. The GUI triggers them
                    # only on explicit user requests (clip_search / face filter).
                self.tm.submit_auto_orient_job(path, all_files)
                self.tm.submit_phash_job(path, all_files)

        def _ledger_batch_cb(paths):
            self.db.ledgers.ledger_batch_insert(paths, scan_root=path)

        reconcile_job = SourceJob(
            job_id=f"gui_scan::{path}",
            priority=Priority(80),
            generator=self.directory_scanner.scan_incremental_reconcile(
                path, recursive, reconcile_ctx
            ),
            task_factory=self.tm.create_gui_tasks_for_file,
            create_tasks=False,
            on_complete=_on_reconcile_complete,
            on_batch_discovered=_ledger_batch_cb,
        )
        self.rm.submit_source_job(reconcile_job)

        # Watch the browsed directory for live filesystem changes so new
        # images are detected even if the path isn't in config watch_paths.
        if self.tm.watchdog_handler:
            self.tm.watchdog_handler.set_gui_directory(path, recursive)

        return {
            'files': sorted(db_files),
            'thumbnail_paths': thumb_map,
        }

    def get_subdirectories(self, parent_path: str, on_folder_card_scanned=None) -> List[FolderNode]:
        """Return FolderNodes for all immediate subdirectories.

        DB rows provide counts and preview thumbnails for indexed dirs.
        A filesystem fallback (os.scandir) picks up dirs not yet in the DB.
        """
        rows = self.db.images.get_subdirectory_info(parent_path)
        db_names = {r['name'] for r in rows}

        # Filesystem fallback: include dirs the DB doesn't know about yet
        try:
            for entry in os.scandir(parent_path):  # disk-io: surface unindexed subdirs as folder cards
                if entry.is_dir(follow_symlinks=False) and entry.name not in db_names:
                    rows.append({
                        'path': entry.path,
                        'name': entry.name,
                        'image_count': 0,
                        'recursive_count': 0,
                        'preview_paths': [],
                    })
        except OSError as e:
            logger.warning("get_subdirectories: scandir failed for %s: %s", parent_path, e)

        all_paths = [r['path'] for r in rows]
        ratings = self.db.images.get_folder_ratings_batch(all_paths) if all_paths else {}

        nodes = []
        for r in sorted(rows, key=lambda r: r['name']):
            image_paths = sorted(self.db.images.get_directory_files(r['path'], recursive=True))
            nodes.append(FolderNode(
                path=r['path'],
                name=r['name'],
                parent_path=parent_path,
                image_count=r['image_count'],
                recursive_count=r['recursive_count'],
                preview_paths=r['preview_paths'],
                image_paths=image_paths,
                rating=ratings.get(r['path'], 0),
            ))
            # Trigger a background scan for any folder that has no indexed images.
            # This covers directories surfaced by the filesystem fallback that the
            # daemon hasn't walked yet.
            if not image_paths:
                self._request_scan_for_folder_card(r['path'], on_images_discovered=on_folder_card_scanned)
        return nodes

    def _request_scan_for_folder_card(self, path: str, on_images_discovered=None) -> None:
        """Submit a low-priority reconcile scan for an unindexed folder card.

        Uses the ``folder_card_scan::`` job prefix so it doesn't conflict with
        the active ``gui_scan::`` job for the currently-viewed directory.
        Deduplicates via the render manager's active_jobs registry.
        """
        job_id = f"folder_card_scan::{path}"
        with self.rm.active_jobs_lock:
            if job_id in self.rm.active_jobs:
                return

        logger.info("[folder-card] scheduling background scan for unindexed folder: %s", path)

        reconcile_ctx = ReconcileContext(db_file_set=set())

        def _on_complete():
            discovered = reconcile_ctx.discovered_files
            if not discovered:
                return
            logger.info("[folder-card] discovered %d files in %s", len(discovered), path)
            discovered_list = sorted(discovered)
            for wt in ('thumbnail', 'view_image', 'metadata'):
                self.db.ledgers.file_work_batch_insert(discovered_list, wt, scan_root=path)

            if on_images_discovered:
                on_images_discovered(path, discovered_list)

            self.rm.submit_source_job(SourceJob(
                job_id=f"folder_card_tasks::{path}",
                priority=Priority.LOW,
                task_priority=Priority.LOW,
                generator=_batch_iter(discovered_list),
                task_factory=self.tm.create_gui_tasks_for_file,
                create_tasks=True,
                suppress_progress=True,
            ))

        def _ledger_cb(paths):
            self.db.ledgers.ledger_batch_insert(paths, scan_root=path)

        self.rm.submit_source_job(SourceJob(
            job_id=job_id,
            priority=Priority.LOW,
            generator=self.directory_scanner.scan_incremental_reconcile(
                path, False, reconcile_ctx
            ),
            task_factory=self.tm.create_gui_tasks_for_file,
            create_tasks=False,
            suppress_progress=True,  # why: this job scans a subdirectory; emitting
            # scan_progress would pass path_belongs_to_current_directory() for the
            # parent folder and inject subdirectory images into the non-recursive grid.
            on_complete=_on_complete,
            on_batch_discovered=_ledger_cb,
        ))

    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool],
                                tag_names: Optional[List[str]] = None,
                                duplicates_only: bool = False,
                                date_range: Optional[Tuple[float, float]] = None,
                                directory: Optional[str] = None,
                                recursive: bool = True) -> List[str]:
        return self.db.images.get_filtered_file_paths(
            text_filter, star_states,
            tag_names=tag_names if tag_names else None,
            duplicates_only=duplicates_only,
            date_range=date_range,
            directory=directory,
            recursive=recursive,
        )

    def get_date_range_for_paths(self, file_paths: List[str]) -> Optional[Tuple[float, float]]:
        return self.db.images.get_date_range_for_paths(file_paths)

    def get_phash_duplicate_paths(self, file_paths: List[str]) -> set:
        from core.phash_coordinator import PHashCoordinator
        pairs = self.db.images.get_phash_pairs(file_paths)
        return PHashCoordinator.find_near_duplicates(pairs)

    def request_phash_batch(self, file_paths: List[str], directory: str,
                            priority: Priority = Priority.GUI_REQUEST) -> None:
        self.tm.submit_phash_job(directory, file_paths, priority)

    # ------------------------------------------------------------------
    #  Thumbnail / Preview
    # ------------------------------------------------------------------

    def request_previews(self, image_paths: List[str], priority: int = 50) -> int:
        return self.tm.batch_request_thumbnails(image_paths, Priority(priority))

    def update_viewport_heatmap(
        self,
        upgrade_pairs: List[tuple],
        paths_to_downgrade: List[str],
        fullres_pairs: List[tuple],
        fullres_to_cancel: List[str],
    ) -> None:
        for path, pri in upgrade_pairs:
            self.tm.request_thumbnail(path, Priority(pri))

        if paths_to_downgrade:
            self.tm.downgrade_thumbnail_tasks(paths_to_downgrade, Priority.GUI_REQUEST_LOW)

        if fullres_to_cancel:
            self.tm.cancel_speculative_fullres_batch(fullres_to_cancel)

        for path, pri in fullres_pairs:
            self.tm.request_speculative_fullres(path, Priority(pri))

    def get_previews_status(self, image_paths: List[str]) -> Dict[str, Dict]:
        statuses = {}
        for path in image_paths:
            is_thumbnail_ready = False
            thumbnail_path = None
            view_image_ready = False
            view_image_path = None

            cached_paths = self.db.images.get_thumbnail_paths(path)
            if cached_paths:
                thumbnail_path = cached_paths.get('thumbnail_path')
                view_image_path = cached_paths.get('view_image_path')
                if view_image_path and os.path.exists(view_image_path):  # disk-io: preview status
                    view_image_ready = True
                if thumbnail_path and os.path.exists(thumbnail_path):  # disk-io: preview status
                    is_thumbnail_ready = True

            statuses[path] = {
                'thumbnail_ready': is_thumbnail_ready,
                'thumbnail_path': thumbnail_path if is_thumbnail_ready else None,
                'view_image_ready': view_image_ready,
                'view_image_path': view_image_path if view_image_ready else None,
            }
        return statuses

    # ------------------------------------------------------------------
    #  View Image (fullres)
    # ------------------------------------------------------------------

    def request_view_image(self, image_path: str) -> Dict[str, Any]:
        """Returns dict with 'view_image_path' and 'view_image_source'."""
        result = self.tm.request_view_image(image_path)
        if result == "memory":
            return {'view_image_path': None, 'view_image_source': 'memory'}
        if isinstance(result, str) and result.startswith("direct:"):
            return {'view_image_path': result[len("direct:"):], 'view_image_source': 'direct'}
        return {
            'view_image_path': result,
            'view_image_source': 'disk' if result else None,
        }

    def get_cached_view_image(self, image_path: str) -> Optional[bytes]:
        return self.tm.fullres_cache.get(image_path)

    # ------------------------------------------------------------------
    #  Metadata
    # ------------------------------------------------------------------

    def get_metadata_batch(self, image_paths: List[str],
                           priority: bool = False) -> Dict[str, Dict]:
        if priority:
            self.rm.submit_task(
                f"metadata_batch::{hash(tuple(image_paths))}",
                Priority.GUI_REQUEST,
                self.tm.request_metadata_extraction,
                image_paths, Priority.GUI_REQUEST,
                task_type=TaskType.SIMPLE,
            )
        return self.db.images.get_metadata_batch(image_paths)

    # ------------------------------------------------------------------
    #  Rating
    # ------------------------------------------------------------------

    def set_rating(self, image_paths: List[str], rating: int) -> bool:
        success, _count = self.db.images.batch_set_ratings(image_paths, rating)
        if success:
            for path in image_paths:
                self.db.ledgers.pending_write_insert(path, 'rating', {'rating': rating})
                self.rm.submit_task(
                    f"write_rating::{path}",
                    Priority.NORMAL,
                    self.tm.write_rating_to_file,
                    path, rating,
                    task_type=TaskType.SIMPLE,
                )
        return success

    def set_folder_rating(self, folder_path: str, rating: int) -> bool:
        return self.db.images.set_folder_rating(folder_path, rating)

    # ------------------------------------------------------------------
    #  OCIO color management
    # ------------------------------------------------------------------

    def set_ocio_assignment(
        self,
        path: str,
        is_folder: bool,
        config_path: str,
        input_space: str,
        display: str = "",
        view: str = "",
    ) -> None:
        """Persist an OCIO assignment and invalidate affected thumbnail caches."""
        self.db.ocio.set(path, is_folder, config_path, input_space, display, view)
        from core.ocio_manager import ocio_manager
        ocio_manager.invalidate_config_cache(config_path)
        if is_folder:
            self._invalidate_ocio_cache_for_folder(path)

    def delete_ocio_assignment(self, path: str) -> None:
        """Remove an OCIO assignment and invalidate affected caches."""
        assignment = self.db.ocio.get_for_file(path)
        config_path = assignment.config_path if assignment else None
        self.db.ocio.delete(path)
        if config_path:
            from core.ocio_manager import ocio_manager
            ocio_manager.invalidate_config_cache(config_path)
        # Invalidate cache for the path itself (file) or its subtree (folder)
        self._invalidate_ocio_cache_for_folder(path)

    def _invalidate_ocio_cache_for_folder(self, folder_path: str) -> None:
        """Delete cached thumbnail/view-image files for all files under *folder_path*.

        Clears the DB columns too so ThumbnailManager queues regeneration.
        Uses the existing remove_records/cache eviction helpers rather than
        duplicating file-delete logic.
        """
        try:
            conn = self.db.images._read_conn()
            rows = conn.execute(
                "SELECT file_path, thumbnail_path, view_image_path "
                "FROM image_metadata "
                "WHERE file_path = ? OR file_path LIKE ?",
                (folder_path, folder_path.rstrip("/") + "/%"),
            ).fetchall()
        except Exception as e:
            logger.warning("OCIO cache invalidation query failed: %s", e)
            return

        if not rows:
            return

        file_paths = [r[0] for r in rows]
        # Delete cache files from disk
        for _fp, thumb, view in rows:
            for cache_path in (thumb, view):
                if cache_path:
                    try:
                        os.remove(cache_path)
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning("Failed to remove cache file %s: %s", cache_path, e)

        # Clear cache columns in DB so regeneration is triggered
        try:
            with self.db.images._lock:
                self.db.images.conn.executemany(
                    "UPDATE image_metadata SET thumbnail_path = NULL, view_image_path = NULL "
                    "WHERE file_path = ?",
                    [(fp,) for fp in file_paths],
                )
                self.db.images._soft_commit()
        except Exception as e:
            logger.warning("OCIO DB cache clear failed: %s", e)

        logger.info(
            "OCIO: invalidated %d cached thumbnails under %s", len(file_paths), folder_path
        )

    # ------------------------------------------------------------------
    #  Rotation
    # ------------------------------------------------------------------

    # Cumulative rotation: compose current orientation with the requested rotation.
    _ROTATE_CW_90 = {1: 6, 2: 7, 3: 8, 4: 5, 5: 2, 6: 3, 7: 4, 8: 1}
    _ROTATE_180 = {1: 3, 2: 4, 3: 1, 4: 2, 5: 7, 6: 8, 7: 5, 8: 6}
    _ROTATE_CW_270 = {1: 8, 2: 5, 3: 6, 4: 7, 5: 4, 6: 1, 7: 2, 8: 3}
    _ROTATION_TABLES = {90: _ROTATE_CW_90, 180: _ROTATE_180, 270: _ROTATE_CW_270}

    def rotate_images(self, image_paths: List[str], degrees: int) -> bool:
        """Rotate images by updating EXIF Orientation (cumulative)."""
        table = self._ROTATION_TABLES.get(degrees)
        if table is None:
            return False

        for path in image_paths:
            current = self.db.images.get_orientation(path)
            new_orientation = table.get(current, table.get(1, 1))
            self.db.images.set_orientation(path, new_orientation)
            self.db.ledgers.pending_write_insert(
                path, 'orientation', {'orientation': new_orientation})
            # Remove any existing write_orientation task (regardless of state)
            # so the new submission with the latest orientation is not ignored.
            task_id = f"write_orientation::{path}"
            with self.rm.graph_lock:
                prev = self.rm.task_graph.pop(task_id, None)
                if prev:
                    prev.is_active = False
            self.rm.submit_task(
                task_id,
                Priority.NORMAL,
                self._write_orientation_to_sidecar,
                path, new_orientation,
                task_type=TaskType.SIMPLE,
            )
        return True

    def set_absolute_orientation(self, image_paths: List[str], orientations: List[int]) -> bool:
        """Set EXIF orientation to exact values (not cumulative). Queues sidecar writes."""
        if len(image_paths) != len(orientations):
            return False
        for path, orientation in zip(image_paths, orientations):
            self.db.images.set_orientation(path, orientation)
            self.db.ledgers.pending_write_insert(
                path, 'orientation', {'orientation': orientation})
            task_id = f"write_orientation::{path}"
            with self.rm.graph_lock:
                prev = self.rm.task_graph.pop(task_id, None)
                if prev:
                    prev.is_active = False
            self.rm.submit_task(
                task_id,
                Priority.NORMAL,
                self._write_orientation_to_sidecar,
                path, orientation,
                task_type=TaskType.SIMPLE,
            )
        return True

    def _write_orientation_to_sidecar(self, path: str, orientation: int):
        """Write orientation to sidecar file only — no cache invalidation."""
        self.tm.write_orientation_to_file(path, orientation)

    def invalidate_thumbnail_paths(self, image_paths: List[str]):
        """Clear DB thumbnail/view paths and delete cached files synchronously.

        After this call, ``is_thumbnail_valid()`` returns False for all given
        paths, preventing heatmap re-requests from reloading stale cached
        files.  Does NOT evict the task graph — completed tasks stay so that
        heatmap ``request_thumbnail`` calls are no-ops (the background task
        handles graph eviction + re-request after the file write completes).
        """
        for path in image_paths:
            self.tm.invalidate_cached_images(path)

    # ------------------------------------------------------------------
    #  Tags
    # ------------------------------------------------------------------

    def set_tags(self, image_paths: List[str], tags: List[str]) -> bool:
        success = self.db.tags.batch_set_tags(image_paths, tags)
        if success:
            self._queue_tag_write_tasks(image_paths)
        return success

    def remove_tags(self, image_paths: List[str], tags: List[str]) -> bool:
        success = self.db.tags.batch_remove_tags(image_paths, tags)
        if success:
            self._queue_tag_write_tasks(image_paths)
        return success

    def _queue_tag_write_tasks(self, image_paths: List[str]) -> None:
        all_tags_map = self.db.tags.batch_get_image_tags(image_paths)
        for path, path_tags in all_tags_map.items():
            self.db.ledgers.pending_write_insert(path, 'tags', {'tags': path_tags})
            self.rm.submit_task(
                f"write_tags::{path}",
                Priority.NORMAL,
                self.tm.write_tags_to_file,
                path, path_tags,
                task_type=TaskType.SIMPLE,
            )

    def get_tags(self, directory_path: str = "") -> Dict[str, List[Dict]]:
        all_tags = self.db.tags.get_all_tags()
        dir_tags = self.db.tags.get_directory_tags(directory_path) if directory_path else []
        return {
            'directory_tags': dir_tags,
            'global_tags': all_tags,
        }

    def get_image_tags(self, image_paths: List[str]) -> Dict[str, List[str]]:
        return {path: self.db.tags.get_image_tags(path) for path in image_paths}

    # ------------------------------------------------------------------
    #  Move Records
    # ------------------------------------------------------------------

    def move_records(self, moves: List[Dict[str, str]]) -> int:
        return self.db.images.move_records(moves)

    # ------------------------------------------------------------------
    #  Run Tasks (compound operations from scripts)
    # ------------------------------------------------------------------

    def run_tasks(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        for op in operations:
            if self.tm.task_ops.get(op['name']) is None:
                return {'status': 'error', 'message': f"Unknown task operation: {op['name']}"}

        with self._counter_lock:
            self._compound_task_counter += 1
            task_id = f"script_task::{self._compound_task_counter}"

        op_tuples = [
            (op['name'], op['file_paths'], op.get('kwargs', {}))
            for op in operations
        ]
        queued = self.rm.submit_task(
            task_id,
            Priority.NORMAL,
            self.tm.task_ops.execute_compound,
            op_tuples,
        )
        if not queued:
            return {'status': 'error', 'message': f"Failed to queue compound task: {task_id}"}
        return {'status': 'success', 'task_id': task_id, 'queued_count': len(operations)}

    # ------------------------------------------------------------------
    #  ComfyUI
    # ------------------------------------------------------------------

    def comfyui_generate(self, image_path: str, prompt: str = "",
                         denoise: float = 0.0, workflow: str = "") -> str:
        return self.tm.submit_comfyui_generation(
            image_path, prompt, denoise, workflow=workflow,
        )

    # ------------------------------------------------------------------
    #  CLIP Search
    # ------------------------------------------------------------------

    def clip_search(self, query: str, threshold: float = 0.0, scope=None):
        """Search images by text query using CLIP embeddings.

        Returns all images with cosine similarity >= threshold, sorted descending.
        scope: optional list of file_paths to restrict search to (e.g. current view).
        Returns [(file_path, similarity_score), ...] or [] if models unavailable.
        """
        from core import clip_inference, clip_search

        query_embedding = clip_inference.encode_text(
            query, config_manager=self.config_manager)
        if query_embedding is None:
            return []

        if scope:
            # Scope provided: fetch only necessary embeddings, no caching
            rows = self.db.embeddings.get_embeddings_for_files(scope)
            file_paths, matrix = clip_search.build_embedding_matrix(rows)
        else:
            # No scope: use global cache
            gen = self.db.embeddings.generation
            with self._clip_cache_lock:
                if self._clip_cache is not None and self._clip_cache_gen == gen:
                    file_paths, matrix = self._clip_cache
                else:
                    rows = self.db.embeddings.get_all_embeddings()
                    file_paths, matrix = clip_search.build_embedding_matrix(rows)
                    if matrix is not None:
                        self._clip_cache = (file_paths, matrix)
                        self._clip_cache_gen = gen

        if matrix is None or len(file_paths) == 0:
            return []

        scores = matrix @ query_embedding

        above_threshold_indices = np.where(scores >= threshold)[0]
        if len(above_threshold_indices) == 0:
            return []

        scores_above_threshold = scores[above_threshold_indices]
        sorted_indices = np.argsort(scores_above_threshold)[::-1]  # descending

        # Map sorted indices back to original file_paths indices
        original_indices = above_threshold_indices[sorted_indices]

        return [(file_paths[i], float(scores[i])) for i in original_indices]

    def find_similar_images(self, source_path: str, threshold: float = 0.5, scope=None):
        """Return images with cosine similarity >= threshold to source_path.

        Returns [(file_path, score), ...] sorted descending, or None if source has no embedding.
        scope: optional list of file_paths to restrict search to.
        """
        from core import clip_search

        blob = self.db.embeddings.get_embedding(source_path)
        if blob is None:
            return None

        np_mod = clip_search._get_numpy()
        if np_mod is None:
            return []

        source_embedding = np_mod.frombuffer(blob, dtype=np_mod.float32).copy()

        if scope:
            rows = self.db.embeddings.get_embeddings_for_files(scope)
            file_paths, matrix = clip_search.build_embedding_matrix(rows)
        else:
            gen = self.db.embeddings.generation
            with self._clip_cache_lock:
                if self._clip_cache is not None and self._clip_cache_gen == gen:
                    file_paths, matrix = self._clip_cache
                else:
                    rows = self.db.embeddings.get_all_embeddings()
                    file_paths, matrix = clip_search.build_embedding_matrix(rows)
                    if matrix is not None:
                        self._clip_cache = (file_paths, matrix)
                        self._clip_cache_gen = gen

        if matrix is None or not file_paths:
            return []

        scores = matrix @ source_embedding

        above_threshold_indices = np.where(scores >= threshold)[0]
        if len(above_threshold_indices) == 0:
            return []

        scores_above_threshold = scores[above_threshold_indices]
        sorted_indices = np.argsort(scores_above_threshold)[::-1]  # descending

        original_indices = above_threshold_indices[sorted_indices]

        return [(file_paths[i], float(scores[i])) for i in original_indices]

    def find_similar_by_phash(self, source_path: str, max_distance: int, scope):
        """Return images within max_distance Hamming distance of source_path's pHash.

        Returns [(file_path, distance), ...] sorted ascending by distance,
        or None if source has no pHash stored.
        scope: required list of file_paths to search within.
        """
        max_distance = int(max_distance)

        source_pairs = self.db.images.get_phash_pairs([source_path])
        if not source_pairs:
            return None
        source_hash = source_pairs[0][1]

        if not scope:
            return []
        pairs = self.db.images.get_phash_pairs(list(scope))
        if not pairs:
            return []

        results = []
        for fp, h in pairs:
            # Mask to 64 bits before counting to handle signed int64 storage
            dist = bin((source_hash ^ h) & 0xFFFFFFFFFFFFFFFF).count('1')
            if dist <= max_distance:
                results.append((fp, dist))
        results.sort(key=lambda x: x[1])
        return results

    def invalidate_clip_cache(self):
        with self._clip_cache_lock:
            self._clip_cache = None
            self._clip_cache_gen = -1

    def clip_index_status(self):
        model = self.config_manager.get("ai.clip_search.model", "clip-vit-b-32")
        indexed = self.db.embeddings.count_embeddings(model)
        return {"indexed": indexed, "model": model}

    # ------------------------------------------------------------------
    #  Face Recognition
    # ------------------------------------------------------------------

    def get_faces_for_file(self, file_path: str) -> list:
        return self.db.faces.get_faces_for_file(file_path)

    def get_all_persons(self, include_hidden: bool = False) -> list:
        return self.db.faces.get_all_persons(include_hidden)

    def get_persons_for_files(self, file_paths: list, include_hidden: bool = False) -> list:
        return self.db.faces.get_persons_for_files(file_paths, include_hidden)

    def rename_person(self, person_id: str, name: str):
        self.db.faces.rename_person(person_id, name)

    def merge_persons(self, target_id: str, source_ids: list):
        self.db.faces.merge_persons(target_id, source_ids)

    def hide_person(self, person_id: str, hidden: bool):
        self.db.faces.hide_person(person_id, hidden)

    def ungroup_person(self, person_id: str):
        self.db.faces.ungroup_person(person_id)

    def set_feature_face(self, person_id: str, face_id: str):
        self.db.faces.set_feature_face(person_id, face_id)

    def get_face_paths_for_person(self, person_id: str) -> list:
        return self.db.faces.get_face_paths_for_person(person_id)

    def get_faces_for_person(self, person_id: str) -> list:
        return self.db.faces.get_faces_for_person(person_id)

    def get_feature_faces_batch(self, person_feature_map: dict) -> dict:
        return self.db.faces.get_feature_faces_batch(person_feature_map)

    def get_face_paths_for_persons(self, person_ids: list) -> list:
        return self.db.faces.get_face_paths_for_persons(person_ids)

    def assign_face_to_person(self, face_id: str, person_id: str):
        self.db.faces.assign_face_to_person(face_id, person_id)

    def get_person_names(self) -> list:
        return self.db.faces.get_person_names()

    def create_person(self, person_id: str, name: str = '', feature_face_id: str = None):
        return self.db.faces.create_person(person_id, name, feature_face_id)

    def get_person_by_name(self, name: str):
        return self.db.faces.get_person_by_name(name)

    def index_faces_on_demand(self, directory: str, file_paths: list) -> None:
        """Submit on-demand face detection for any unindexed files in file_paths."""
        missing = self.db.faces.get_files_missing_faces(file_paths)
        if missing:
            self.tm.submit_face_detection_job(directory, missing, on_demand=True)

    def index_clip_on_demand(self, directory: str, file_paths: list) -> None:
        """Submit CLIP indexing for any unindexed files in file_paths."""
        missing = self.db.embeddings.get_files_missing_embeddings(file_paths)
        if missing:
            self.tm.submit_clip_indexing_job(directory, missing)

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def recover_pending_writes(self) -> int:
        """Resubmit pending file writes from a prior session."""
        return self.tm.recover_pending_writes()

    def shutdown(self):
        self.tm.shutdown()
