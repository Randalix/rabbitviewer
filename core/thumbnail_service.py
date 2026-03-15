"""In-process facade for the GUI.

Delegates to ThumbnailManager / MetadataDatabase / RenderManager directly.
"""
import logging
import os
import threading
import time
from typing import List, Optional, Dict, Any

from core.directory_scanner import DirectoryScanner, ReconcileContext
from core.notifications import Notification, FilesRemovedData, ImageEntryModel
from core.rendermanager import Priority, TaskType, SourceJob

logger = logging.getLogger(__name__)


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
        db_files = self.db.get_directory_files(path, recursive=recursive)
        logger.info(
            f"DB returned {len(db_files)} cached files for '{path}' "
            f"(recursive={recursive}). Starting reconciliation walk."
        )

        thumb_map: Dict[str, str] = {}
        if db_files:
            t0 = time.perf_counter()
            validity = self.db.batch_get_cached_thumbnail_validity(db_files)
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
                    self.db.file_work_batch_insert(discovered_list, wt, scan_root=path)

                def _discovered_batch_generator():
                    batch = []
                    for f in discovered:
                        batch.append(f)
                        if len(batch) >= 10:
                            yield batch
                            batch = []
                    if batch:
                        yield batch

                task_job = SourceJob(
                    job_id=f"post_scan::{path}",
                    priority=Priority.LOW,
                    task_priority=Priority.LOW,
                    generator=_discovered_batch_generator(),
                    task_factory=self.tm.create_gui_tasks_for_file,
                    create_tasks=True,
                )
                self.rm.submit_source_job(task_job)

            self.db.ledger_prune_complete(path)

            # Chain AI background jobs after scan completes
            all_files = list(discovered) + [f for f in db_files if f not in discovered]
            if all_files:
                cfg = self.tm.config_manager
                if cfg.get("ai.enabled", True):
                    if cfg.get("ai.clip_search.enabled", True):
                        self.db.file_work_batch_insert(all_files, 'clip', scan_root=path)
                    if cfg.get("ai.auto_orient.enabled", False):
                        self.db.file_work_batch_insert(all_files, 'auto_orient', scan_root=path)
                    if cfg.get("ai.face_recognition.enabled", True):
                        self.db.file_work_batch_insert(all_files, 'face_detect', scan_root=path)
                self.tm.submit_clip_indexing_job(path, all_files)
                self.tm.submit_auto_orient_job(path, all_files)
                self.tm.submit_face_detection_job(path, all_files)

        def _ledger_batch_cb(paths):
            self.db.ledger_batch_insert(paths, scan_root=path)

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

    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool],
                                tag_names: Optional[List[str]] = None) -> List[str]:
        return self.db.get_filtered_file_paths(
            text_filter, star_states,
            tag_names=tag_names if tag_names else None,
        )

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

            cached_paths = self.db.get_thumbnail_paths(path)
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
        return self.tm._mem_cache_get(image_path)

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
        return self.db.get_metadata_batch(image_paths)

    # ------------------------------------------------------------------
    #  Rating
    # ------------------------------------------------------------------

    def set_rating(self, image_paths: List[str], rating: int) -> bool:
        success, _count = self.db.batch_set_ratings(image_paths, rating)
        if success:
            for path in image_paths:
                self.db.pending_write_insert(path, 'rating', {'rating': rating})
                self.rm.submit_task(
                    f"write_rating::{path}",
                    Priority.NORMAL,
                    self.tm.write_rating_to_file,
                    path, rating,
                    task_type=TaskType.SIMPLE,
                )
        return success

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
            current = self.db.get_orientation(path)
            new_orientation = table.get(current, table.get(1, 1))
            self.db.set_orientation(path, new_orientation)
            self.db.pending_write_insert(
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
            self.db.set_orientation(path, orientation)
            self.db.pending_write_insert(
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
        success = self.db.batch_set_tags(image_paths, tags)
        if success:
            self._queue_tag_write_tasks(image_paths)
        return success

    def remove_tags(self, image_paths: List[str], tags: List[str]) -> bool:
        success = self.db.batch_remove_tags(image_paths, tags)
        if success:
            self._queue_tag_write_tasks(image_paths)
        return success

    def _queue_tag_write_tasks(self, image_paths: List[str]) -> None:
        all_tags_map = self.db.batch_get_image_tags(image_paths)
        for path, path_tags in all_tags_map.items():
            self.db.pending_write_insert(path, 'tags', {'tags': path_tags})
            self.rm.submit_task(
                f"write_tags::{path}",
                Priority.NORMAL,
                self.tm.write_tags_to_file,
                path, path_tags,
                task_type=TaskType.SIMPLE,
            )

    def get_tags(self, directory_path: str = "") -> Dict[str, List[Dict]]:
        all_tags = self.db.get_all_tags()
        dir_tags = self.db.get_directory_tags(directory_path) if directory_path else []
        return {
            'directory_tags': dir_tags,
            'global_tags': all_tags,
        }

    def get_image_tags(self, image_paths: List[str]) -> Dict[str, List[str]]:
        return {path: self.db.get_image_tags(path) for path in image_paths}

    # ------------------------------------------------------------------
    #  Move Records
    # ------------------------------------------------------------------

    def move_records(self, moves: List[Dict[str, str]]) -> int:
        return self.db.move_records(moves)

    # ------------------------------------------------------------------
    #  Run Tasks (compound operations from scripts)
    # ------------------------------------------------------------------

    def run_tasks(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        for op in operations:
            if self.tm.get_task_operation(op['name']) is None:
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
            self.tm.execute_compound_task,
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

    def clip_search(self, query: str, top_k: int = 50, scope=None):
        """Search images by text query using CLIP embeddings.

        scope: optional list of file_paths to restrict search to (e.g. current view).
        Returns [(file_path, similarity_score), ...] sorted by score descending.
        """
        from core import clip_inference, clip_search

        query_embedding = clip_inference.encode_text(
            query, config_manager=self.config_manager)
        if query_embedding is None:
            return []

        # Reuse cached matrix if no embeddings were added/removed since last build
        gen = self.db.embedding_generation
        if self._clip_cache is not None and self._clip_cache_gen == gen:
            file_paths, matrix = self._clip_cache
        else:
            rows = self.db.get_all_embeddings()
            file_paths, matrix = clip_search.build_embedding_matrix(rows)
            if matrix is not None:
                self._clip_cache = (file_paths, matrix)
                self._clip_cache_gen = gen

        if matrix is None:
            return []

        # Filter to scope if provided
        if scope is not None:
            np = clip_search._get_numpy()
            scope_set = set(scope)
            mask = [i for i, fp in enumerate(file_paths) if fp in scope_set]
            if not mask:
                return []
            file_paths = [file_paths[i] for i in mask]
            matrix = matrix[np.array(mask)]

        return clip_search.search(query_embedding, file_paths, matrix, top_k=top_k)

    def invalidate_clip_cache(self):
        self._clip_cache = None
        self._clip_cache_gen = -1

    def clip_index_status(self):
        model = self.config_manager.get("ai.clip_search.model", "clip-vit-b-32")
        indexed = self.db.count_embeddings(model)
        return {"indexed": indexed, "model": model}

    # ------------------------------------------------------------------
    #  Face Recognition
    # ------------------------------------------------------------------

    def get_faces_for_file(self, file_path: str) -> list:
        return self.db.get_faces_for_file(file_path)

    def get_all_persons(self, include_hidden: bool = False) -> list:
        return self.db.get_all_persons(include_hidden)

    def get_persons_for_files(self, file_paths: list, include_hidden: bool = False) -> list:
        return self.db.get_persons_for_files(file_paths, include_hidden)

    def rename_person(self, person_id: str, name: str):
        self.db.rename_person(person_id, name)

    def merge_persons(self, target_id: str, source_ids: list):
        self.db.merge_persons(target_id, source_ids)

    def hide_person(self, person_id: str, hidden: bool):
        self.db.hide_person(person_id, hidden)

    def ungroup_person(self, person_id: str):
        self.db.ungroup_person(person_id)

    def set_feature_face(self, person_id: str, face_id: str):
        self.db.set_feature_face(person_id, face_id)

    def get_face_paths_for_person(self, person_id: str) -> list:
        return self.db.get_face_paths_for_person(person_id)

    def get_faces_for_person(self, person_id: str) -> list:
        return self.db.get_faces_for_person(person_id)

    def get_feature_faces_batch(self, person_feature_map: dict) -> dict:
        return self.db.get_feature_faces_batch(person_feature_map)

    def get_face_paths_for_persons(self, person_ids: list) -> list:
        return self.db.get_face_paths_for_persons(person_ids)

    def assign_face_to_person(self, face_id: str, person_id: str):
        self.db.assign_face_to_person(face_id, person_id)

    def get_person_names(self) -> list:
        return self.db.get_person_names()

    def create_person(self, person_id: str, name: str = '', feature_face_id: str = None):
        return self.db.create_person(person_id, name, feature_face_id)

    def get_person_by_name(self, name: str):
        return self.db.get_person_by_name(name)

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def recover_pending_writes(self) -> int:
        """Resubmit pending file writes from a prior session."""
        return self.tm.recover_pending_writes()

    def shutdown(self):
        self.tm.shutdown()
