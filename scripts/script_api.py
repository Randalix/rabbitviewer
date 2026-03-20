# scripts/script_api.py
import itertools
import logging
import time

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import List, Optional, Set, Dict
from core.event_system import event_system, EventType, StatusMessageEventData, StatusSection, ThumbnailOverlayEventData
from core.file_grouping import FileGroup, expand_paths_for_group

_overlay_id_counter = itertools.count()
from core.selection import ReplaceSelectionCommand, AddToSelectionCommand

class ScriptAPI:
    """API interface provided to scripts for interacting with RabbitViewer."""

    def __init__(self, main_window, main_thread_invoke=None):
        """
        Initialize the Script API.

        Args:
            main_window: Reference to the MainWindow instance
            main_thread_invoke: Callable that dispatches a function to the Qt main thread
        """
        self.main_window = main_window
        self._invoke = main_thread_invoke
        self._last_operation_time = 0
        self._operation_stats = {}

    def _on_main_thread(self, fn):
        """Run fn on the Qt main thread. Blocks until complete."""
        if self._invoke is None:
            fn()
            return
        import threading
        if threading.current_thread() is threading.main_thread():
            fn()
            return
        done = threading.Event()
        result = [None]
        error = [None]

        def _wrapper():
            try:
                result[0] = fn()
            except Exception as e:  # why: captures any main-thread exception to re-raise on the calling thread
                error[0] = e
            finally:
                done.set()

        self._invoke(_wrapper)
        done.wait()
        if error[0] is not None:
            raise error[0]
        return result[0]

    def get_hovered_image(self) -> Optional[str]:
        """Get the path of the currently hovered image."""
        def _get():
            if self.main_window.stacked_widget.currentWidget() == self.main_window.picture_view:
                return self.main_window.picture_view.current_path
            elif self.main_window.stacked_widget.currentWidget() == self.main_window.thumbnail_view:
                return self.main_window.thumbnail_view.get_hovered_image_path()
            return None
        return self._on_main_thread(_get)

    def get_selected_images(self) -> Set[str]:
        """Get paths of all currently selected images (excludes folders).

        Delegates to MainWindow.get_effective_selection() which returns
        the explicit selection, falling back to the hovered image.
        """
        def _get():
            paths = set(self.main_window.get_effective_selection())
            folder_paths = self.main_window.thumbnail_view.folder_paths
            return paths - folder_paths if folder_paths else paths
        return self._on_main_thread(_get)

    def get_selected_folders(self) -> Set[str]:
        """Get paths of all currently selected folders."""
        def _get():
            paths = set(self.main_window.get_effective_selection())
            folder_paths = self.main_window.thumbnail_view.folder_paths
            return paths & folder_paths if folder_paths else set()
        return self._on_main_thread(_get)

    def get_benchmark_results(self) -> Dict[str, float]:
        """Return comprehensive benchmark results."""
        def _get():
            view = self.main_window.thumbnail_view
            return view.get_benchmark_results() if view else {}
        results = self._on_main_thread(_get)
        results['last_operation_time'] = self._last_operation_time
        results.update(self._operation_stats)
        return results

    def remove_images(self, image_paths: List[str]) -> None:
        """Remove images from the current view by delegating to the main window."""
        start_time = time.time()
        try:
            if not self.main_window or not image_paths:
                return
            self._on_main_thread(lambda: self.main_window.remove_images(image_paths))
            self._last_operation_time = time.time() - start_time
            self._operation_stats['remove_images_time'] = self._last_operation_time
            self._operation_stats['images_removed'] = len(image_paths)
        except Exception as e:  # why: user scripts may pass garbage; main_window may be mid-teardown
            logger.error(f"Error in remove_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['remove_images_error'] = str(e)

    def remove_image_records(self, image_paths: List[str]) -> bool:
        """Remove image records from database & cache via a background daemon task."""
        return self.daemon_tasks([("remove_records", image_paths)])

    def daemon_tasks(self, operations: List[tuple]) -> bool:
        """Submit compound task operations to the daemon for async execution.

        Each tuple is ``(name, file_paths)`` or ``(name, file_paths, kwargs)``.
        """
        try:
            ops = []
            for op in operations:
                d = {"name": op[0], "file_paths": op[1]}
                if len(op) > 2:
                    d["kwargs"] = op[2]
                ops.append(d)
            response = self.main_window.service.run_tasks(ops)
            if response is None:
                logger.error("daemon_tasks failed: no response (connection issue)")
                return False
            logger.info(f"Submitted {len(ops)} daemon task(s): {[op['name'] for op in ops]}")
            return True
        except Exception as e:  # why: service raises ConnectionError/OSError on IPC failure
            logger.error(f"Error submitting daemon tasks: {e}", exc_info=True)
            return False

    def add_images(self, image_paths: List[str]) -> None:
        """Add new images to the current view."""
        start_time = time.time()
        try:
            view = self.main_window.thumbnail_view
            if not view or not image_paths:
                return
            self._on_main_thread(lambda: view.add_images(image_paths))
            self._last_operation_time = time.time() - start_time
            self._operation_stats['add_images_time'] = self._last_operation_time
            self._operation_stats['images_added'] = len(image_paths)
            logger.debug(f"add_images: {len(image_paths)} images in {self._last_operation_time:.3f}s")
        except Exception as e:  # why: user scripts may pass garbage; view may be mid-teardown
            logger.error(f"Error in add_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['add_images_error'] = str(e)

    def set_selected_images(self, image_paths: List[str], clear_existing: bool = True) -> None:
        """Set the selection state for specified images using the central selection system."""
        start_time = time.time()
        try:
            def _do():
                view = self.main_window.thumbnail_view
                if not view or not hasattr(view, 'all_files') or not view.all_files:
                    return
                paths_to_select = {str(Path(p).absolute()) for p in image_paths} & view._all_files_set
                if not paths_to_select:
                    return
                selection_processor = self.main_window.selection_processor
                if clear_existing:
                    command = ReplaceSelectionCommand(paths=paths_to_select, source="script", timestamp=time.time())
                else:
                    command = AddToSelectionCommand(paths=paths_to_select, source="script", timestamp=time.time())
                selection_processor.process_command(command)
                if hasattr(view, 'ensure_visible') and paths_to_select:
                    first_path = min(paths_to_select)
                    first_idx = view._path_to_idx.get(first_path)
                    if first_idx is not None:
                        view.ensure_visible(first_idx, center=True)
            self._on_main_thread(_do)
            self._last_operation_time = time.time() - start_time
        except Exception as e:  # why: user scripts may pass garbage; view may be mid-teardown
            logger.error(f"Error in set_selected_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['set_selection_error'] = str(e)

    def get_all_images(self) -> List[str]:
        """Get paths of all images currently loaded in the viewer (excludes folders)."""
        try:
            def _get():
                view = self.main_window.thumbnail_view
                if not view:
                    return []
                folder_paths = view.folder_paths
                return [str(Path(path).absolute()) for path in view.current_files
                        if path not in folder_paths]
            return self._on_main_thread(_get)
        except Exception as e:  # why: user scripts may pass garbage; view may be mid-teardown
            logger.error(f"Error in get_all_images: {e}", exc_info=True)
            return []

    def set_image_order(self, ordered_paths: List[str]) -> None:
        """Reorder images in the thumbnail view to match *ordered_paths*."""
        try:
            def _do():
                view = self.main_window.thumbnail_view
                if not view:
                    return
                view.reorder_files(ordered_paths)
            self._on_main_thread(_do)
        except Exception as e:  # why: user scripts may pass invalid paths or thumbnail_view may be mid-teardown
            logger.error(f"Error in set_image_order: {e}", exc_info=True)

    def get_metadata_batch(self, image_paths: List[str]) -> dict:
        """Fetch metadata for a batch of images from the daemon.

        Returns a dict mapping path → metadata dict.  Returns an empty
        dict on failure.
        """
        try:
            resp = self.main_window.service.get_metadata_batch(image_paths)
            return resp if resp else {}
        except Exception as e:  # why: service may raise on internal error
            logger.error(f"Error in get_metadata_batch: {e}", exc_info=True)
            return {}

    def set_rating_for_images(self, image_paths: List[str], rating: int) -> None:
        """
        Sets image ratings using the new socket client API.
        
        Args:
            image_paths: List of image paths to set the rating for.
            rating: The rating to set (0-5).
        """
        if not (0 <= rating <= 5):
            logger.error(f"Invalid rating value: {rating}. Must be between 0 and 5.")
            return

        if not image_paths:
            logger.debug("set_rating_for_images: no images provided.")
            return

        # Cascade rating to paired RAW files when group mode is active.
        all_paths = self.expand_group_paths(image_paths)
        num_images = len(all_paths)
        logger.debug(f"set_rating_for_images: rating={rating} for {num_images} images.")
        start_time = time.time()

        # The new API handles DB updates and file writes in one call
        success = self.main_window.service.set_rating(all_paths, rating)

        duration = time.time() - start_time

        if success:
            logger.debug(
                f"set_rating_for_images: {num_images} images rated in {duration:.2f}s."
            )
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message=f"Finished rating {num_images} images.", timeout=5000
            ))
            def _gui_update():
                self._update_status_bar_rating_if_visible(image_paths, rating)
                tv = self.main_window.thumbnail_view
                if tv and tv.filter_affects_rating():
                    tv.reapply_filters()
            self._on_main_thread(_gui_update)
        else:
            logger.error(f"ScriptAPI: Failed to set rating.")
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message="Failed to set rating for images.", timeout=5000
            ))

    def invalidate_thumbnails(self, image_paths: List[str], clear_gui: bool = True) -> None:
        """Evict cached thumbnail state for the given paths.

        When *clear_gui* is True (default), the grid immediately shows
        placeholders.  When False, the old thumbnail stays visible until
        a ``previews_ready`` notification delivers the replacement — this
        avoids a blank/stale flash when the background task (e.g. rotation)
        generates the correct thumbnail shortly after.
        """
        def _do():
            view = self.main_window.thumbnail_view
            if view:
                view.invalidate_thumbnails(image_paths, clear_labels=clear_gui)
        self._on_main_thread(_do)
        self.main_window.service.invalidate_thumbnail_paths(image_paths)

    def rotate_thumbnails(self, image_paths: List[str], degrees: int) -> None:
        """Visually rotate thumbnails in-place without regeneration."""
        def _do():
            view = self.main_window.thumbnail_view
            if view:
                view.rotate_thumbnails(image_paths, degrees)
        self._on_main_thread(_do)

    def rotate_images(self, image_paths: List[str], degrees: int) -> None:
        """Rotate images by the given degrees (90, 180, or 270) clockwise.

        Updates EXIF Orientation cumulatively, regenerates cached thumbnails.
        """
        if degrees not in (90, 180, 270):
            logger.error(f"Invalid rotation: {degrees}°. Must be 90, 180, or 270.")
            return

        if not image_paths:
            return

        # DB + sidecar update (background). No cache invalidation.
        success = self.main_window.service.rotate_images(image_paths, degrees)

        # Immediate visual rotation in PySide (no NAS round-trip).
        def _gui_update():
            pv = self.main_window.picture_view
            if pv and self.main_window.stacked_widget.currentWidget() is pv:
                if pv.current_path in image_paths:
                    pv.rotate_current_image(degrees)
            tv = self.main_window.thumbnail_view
            if tv:
                tv.rotate_thumbnails(image_paths, degrees)
        self._on_main_thread(_gui_update)

        if success:
            logger.info(f"Rotated {len(image_paths)} images by {degrees}°.")
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message=f"Rotated {len(image_paths)} images {degrees}°.", timeout=3000
            ))
        else:
            logger.error("Failed to rotate images.")
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message="Failed to rotate images.", timeout=5000
            ))



    def set_tags_for_images(self, image_paths: List[str], tags: List[str]) -> None:
        """Adds tags to the given images via the daemon."""
        if not image_paths or not tags:
            return
        success = self.main_window.service.set_tags(image_paths, tags)
        if success:
            logger.debug(f"set_tags_for_images: {len(tags)} tags set on {len(image_paths)} images.")
        else:
            logger.error("ScriptAPI: Failed to set tags.")

    def remove_tags_from_images(self, image_paths: List[str], tags: List[str]) -> None:
        """Removes tags from the given images via the daemon."""
        if not image_paths or not tags:
            return
        success = self.main_window.service.remove_tags(image_paths, tags)
        if success:
            logger.debug(f"remove_tags_from_images: {len(tags)} tags removed from {len(image_paths)} images.")
        else:
            logger.error("ScriptAPI: Failed to remove tags.")

    def get_image_tags(self, image_paths: List[str]) -> dict:
        """Returns {path: [tag_names]} for the given images."""
        if not image_paths:
            return {}
        response = self.main_window.service.get_image_tags(image_paths)
        return response if response else {}

    def show_overlay(self, image_paths: List[str], renderer: str,
                     params: dict = None, position: str = "center",
                     duration: int = 1500, overlay_id: str = None) -> None:
        """duration=None for permanent overlays."""
        overlay_id = overlay_id or f"{renderer}_{next(_overlay_id_counter)}"
        event_system.publish(ThumbnailOverlayEventData(
            event_type=EventType.THUMBNAIL_OVERLAY,
            source="script_api",
            timestamp=time.time(),
            action="show",
            paths=list(image_paths),
            overlay_id=overlay_id,
            renderer_name=renderer,
            params=params or {},
            position=position,
            duration=duration,
        ))

    def remove_overlay(self, image_paths: List[str], overlay_id: str) -> None:
        event_system.publish(ThumbnailOverlayEventData(
            event_type=EventType.THUMBNAIL_OVERLAY,
            source="script_api",
            timestamp=time.time(),
            action="remove",
            paths=list(image_paths),
            overlay_id=overlay_id,
        ))

    def show_message(self, message: str, timeout: int = 5000) -> None:
        """Displays a message in the status bar."""
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="script_api",
            timestamp=time.time(),
            message=message,
            timeout=timeout
        ))

    # -- RAW+JPG group mode helpers ----------------------------------------

    def is_group_mode(self) -> bool:
        def _get():
            view = self.main_window.thumbnail_view
            return bool(view and view.group_mode)
        return self._on_main_thread(_get)

    def get_selected_groups(self) -> List[FileGroup]:
        """Return a FileGroup for each selected image.

        When group mode is off (or a file has no group), a synthetic
        single-file group is returned so callers always get a uniform type.
        """
        def _get():
            view = self.main_window.thumbnail_view
            selected = self.get_selected_images()
            groups: List[FileGroup] = []
            seen_primaries: Set[str] = set()
            for path in sorted(selected):
                group = view.get_group_for_path(path) if view else None
                if group:
                    if group.primary not in seen_primaries:
                        seen_primaries.add(group.primary)
                        groups.append(group)
                else:
                    groups.append(FileGroup(primary=path, raw_files=()))
            return groups
        return self._on_main_thread(_get)

    def expand_group_paths(self, paths: List[str]) -> List[str]:
        """Expand primary paths to include paired RAW files (group mode only)."""
        def _get():
            view = self.main_window.thumbnail_view
            if not view or not view.group_mode:
                return list(paths)
            return expand_paths_for_group(paths, view.group_map)
        return self._on_main_thread(_get)

    def _update_status_bar_rating_if_visible(self, image_paths: List[str], rating: int) -> None:
        """If the currently displayed image was just rated, push the new rating to the status bar."""
        rated = set(image_paths)
        mw = self.main_window
        # Determine which path is currently shown
        visible_path = None
        if mw.picture_view and mw.stacked_widget.currentWidget() is mw.picture_view:
            visible_path = mw.picture_view.current_path
        elif mw.thumbnail_view:
            visible_path = mw.thumbnail_view.get_hovered_image_path()
        if visible_path and visible_path in rated:
            mw.notify_rating_set()
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message=str(rating),
                section=StatusSection.RATING,
            ))

