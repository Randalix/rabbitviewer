# scripts/script_api.py
import itertools
import logging
import time
from pathlib import Path
from typing import List, Optional, Set, Dict
from core.event_system import event_system, EventType, StatusMessageEventData, StatusSection, ThumbnailOverlayEventData

_overlay_id_counter = itertools.count()
from core.selection import ReplaceSelectionCommand, AddToSelectionCommand
from core.priority import Priority

class ScriptAPI:
    """API interface provided to scripts for interacting with RabbitViewer."""
    
    def __init__(self, main_window):
        """
        Initialize the Script API.
        
        Args:
            main_window: Reference to the MainWindow instance
        """
        self.main_window = main_window
        self.socket_client = main_window.socket_client
        self._last_operation_time = 0
        self._operation_stats = {}

    def get_hovered_image(self) -> Optional[str]:
        """Get the path of the currently hovered image."""
        if self.main_window.stacked_widget.currentWidget() == self.main_window.picture_view:
            return self.main_window.picture_view.current_path
        elif self.main_window.stacked_widget.currentWidget() == self.main_window.thumbnail_view:
            # Direct call to the method in ThumbnailViewWidget
            return self.main_window.thumbnail_view.get_hovered_image_path()
        return None
        
    def get_selected_images(self) -> Set[str]:
        """Get paths of all currently selected images."""
        if self.main_window.stacked_widget.currentWidget() == self.main_window.picture_view:
            current_path = self.main_window.picture_view.current_path
            return {current_path} if current_path else set()

        view = self.main_window.thumbnail_view
        if not (view and hasattr(view, 'all_files')):
            return set()

        selected_images = set(self.main_window.selection_state.selected_paths)
        
        # If no images are selected, return the hovered image as a fallback.
        if not selected_images:
            hovered_image = self.get_hovered_image()
            if hovered_image:
                return {hovered_image}
        
        return selected_images

    def get_benchmark_results(self) -> Dict[str, float]:
        """Return comprehensive benchmark results."""
        view = self.main_window.thumbnail_view
        results = view.get_benchmark_results() if view else {}
        results['last_operation_time'] = self._last_operation_time
        results.update(self._operation_stats)
        return results

    def remove_images(self, image_paths: List[str]) -> None:
        """
        Remove images from the current view by delegating to the main window.
        
        Args:
            image_paths: List of paths to images to remove
        """
        start_time = time.time()
        
        try:
            if not self.main_window or not image_paths:
                return
            
            # Delegate to the main window's method to correctly handle view updates.
            self.main_window.remove_images(image_paths)
            
            # Update benchmark stats
            self._last_operation_time = time.time() - start_time
            self._operation_stats['remove_images_time'] = self._last_operation_time
            self._operation_stats['images_removed'] = len(image_paths)

        except Exception as e:
            logging.error(f"Error in remove_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['remove_images_error'] = str(e)

    def remove_image_records(self, image_paths: List[str]) -> bool:
        """Remove image records from database & cache via a background task."""
        try:
            core_tm = self.main_window.core_thumbnail_manager
            if not (core_tm and core_tm.db and core_tm.render_manager):
                logging.error("Core services (db, render_manager) not available.")
                return False

            # Submit the database and cache cleanup as a low-priority background task
            # to ensure it completes properly, even during app shutdown.
            task_id = f"delete-records-{time.monotonic()}"
            core_tm.render_manager.submit_task(
                task_id,
                Priority.LOW,
                core_tm.db.remove_records,
                image_paths
            )
            logging.info(f"Submitted background task {task_id} to delete {len(image_paths)} DB records.")
            return True
        except Exception as e:
            logging.error(f"Error submitting background deletion task: {e}", exc_info=True)
            return False

    def daemon_tasks(self, operations: List[tuple]) -> bool:
        """Submit compound task operations to the daemon for async execution.

        Each tuple is ``(operation_name, file_paths)`` where *operation_name*
        is a key in the daemon's task operation registry (e.g. ``"send2trash"``,
        ``"remove_records"``).
        """
        try:
            from network.protocol import TaskOperation
            ops = [TaskOperation(name=name, file_paths=paths) for name, paths in operations]
            response = self.socket_client.run_tasks(ops)
            if response is None:
                logging.error("daemon_tasks failed: no response (connection issue)")
                return False
            if response.status == "success":
                logging.info(f"Submitted {len(ops)} daemon task(s): {[op.name for op in ops]}")
                return True
            logging.error(f"daemon_tasks failed: {getattr(response, 'message', response)}")
            return False
        except Exception as e:
            logging.error(f"Error submitting daemon tasks: {e}", exc_info=True)
            return False

    def add_images(self, image_paths: List[str]) -> None:
        """Add new images to the current view."""
        start_time = time.time()
        try:
            view = self.main_window.thumbnail_view
            if not view or not image_paths:
                return
            view.add_images(image_paths)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['add_images_time'] = self._last_operation_time
            self._operation_stats['images_added'] = len(image_paths)
            logging.debug(f"add_images: {len(image_paths)} images in {self._last_operation_time:.3f}s")
        except Exception as e:
            logging.error(f"Error in add_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['add_images_error'] = str(e)

    def set_selected_images(self, image_paths: List[str], clear_existing: bool = True) -> None:
        """
        Set the selection state for specified images using the central selection system.
        
        Args:
            image_paths: List of image paths to select
            clear_existing: If True, clears existing selection before setting new one.
                            If False, adds to the existing selection.
        """
        start_time = time.time()
        
        try:
            view = self.main_window.thumbnail_view
            if not view or not hasattr(view, 'all_files') or not view.all_files:
                logging.warning("set_selected_images: Thumbnail view or files not available.")
                return

            # Normalize input paths to absolute paths and filter to known files
            paths_to_select = {str(Path(p).absolute()) for p in image_paths} & view._all_files_set

            if not paths_to_select:
                logging.debug("set_selected_images: No valid paths to select from provided paths.")
                return

            # Use the selection command system
            selection_processor = self.main_window.selection_processor

            if clear_existing:
                command = ReplaceSelectionCommand(paths=paths_to_select, source="script", timestamp=time.time())
            else:
                command = AddToSelectionCommand(paths=paths_to_select, source="script", timestamp=time.time())

            selection_processor.process_command(command)

            # Ensure the first selected image is visible
            if view and hasattr(view, 'ensure_visible') and paths_to_select:
                first_path = min(paths_to_select)
                first_idx = view._path_to_idx.get(first_path)
                if first_idx is not None:
                    view.ensure_visible(first_idx, center=True)

            # Update benchmark stats and log
            self._last_operation_time = time.time() - start_time
            self._operation_stats['set_selection_time'] = self._last_operation_time
            self._operation_stats['images_selected'] = len(paths_to_select)
            logging.debug(f"set_selected_images: {len(paths_to_select)} images in {self._last_operation_time:.3f}s")
            
        except Exception as e:
            logging.error(f"Error in set_selected_images: {e}", exc_info=True)
            self._last_operation_time = time.time() - start_time
            self._operation_stats['set_selection_error'] = str(e)

    def get_all_images(self) -> List[str]:
        """
        Get paths of all images currently loaded in the viewer.
        
        Returns:
            List[str]: List of absolute paths to all images
        """
        try:
            view = self.main_window.thumbnail_view
            if not view:
                return []
                
            # Return list of all image paths from current_files
            return [str(Path(path).absolute()) for path in view.current_files]
            
        except Exception as e:
            logging.error(f"Error in get_all_images: {e}", exc_info=True)
            return []

    def set_image_order(self, ordered_paths: List[str]) -> None:
        """Reorder images in the thumbnail view to match *ordered_paths*.

        This is an atomic operation — no remove+add flicker.
        """
        try:
            view = self.main_window.thumbnail_view
            if not view:
                return
            view.reorder_files(ordered_paths)
        except Exception as e:  # why: user scripts may pass invalid paths or thumbnail_view may be mid-teardown
            logging.error(f"Error in set_image_order: {e}", exc_info=True)

    def get_metadata_batch(self, image_paths: List[str]) -> dict:
        """Fetch metadata for a batch of images from the daemon.

        Returns a dict mapping path → metadata dict.  Returns an empty
        dict on failure.
        """
        try:
            resp = self.socket_client.get_metadata_batch(image_paths)
            if resp and hasattr(resp, "metadata"):
                return resp.metadata
            return {}
        except Exception as e:  # why: socket_client may raise on connection loss or malformed response
            logging.error(f"Error in get_metadata_batch: {e}", exc_info=True)
            return {}

    def set_rating_for_images(self, image_paths: List[str], rating: int) -> None:
        """
        Sets image ratings using the new socket client API.
        
        Args:
            image_paths: List of image paths to set the rating for.
            rating: The rating to set (0-5).
        """
        if not (0 <= rating <= 5):
            logging.error(f"Invalid rating value: {rating}. Must be between 0 and 5.")
            return

        if not image_paths:
            logging.debug("set_rating_for_images: no images provided.")
            return

        num_images = len(image_paths)
        logging.debug(f"set_rating_for_images: rating={rating} for {num_images} images.")
        start_time = time.time()

        # The new API handles DB updates and file writes in one call
        response = self.socket_client.set_rating(image_paths, rating)

        duration = time.time() - start_time

        if response and response.status == "success":
            logging.debug(
                f"set_rating_for_images: {num_images} images rated in {duration:.2f}s."
            )
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message=f"Finished rating {num_images} images.", timeout=5000
            ))
            # Update the rating section if the visible image was just rated
            self._update_status_bar_rating_if_visible(image_paths, rating)
            # Only reapply filters when a star or text filter is active — otherwise
            # reapply_filters() triggers a full grid rebuild for no visible benefit
            # and can cause spurious layout shifts by picking up unrelated DB changes.
            tv = self.main_window.thumbnail_view
            if tv and tv.filter_affects_rating():
                tv.reapply_filters()
        else:
            logging.error(f"ScriptAPI: Failed to set rating. Response: {response}")
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE, source="script_api",
                timestamp=time.time(), message="Failed to set rating for images.", timeout=5000
            ))

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

