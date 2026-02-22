from typing import Optional, Set, List
import threading
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QStackedWidget, QApplication, QFileDialog, QMessageBox
from PySide6.QtCore import Qt, Slot, QPointF, QSize, QPoint, QTimer, QEvent, QObject, Signal, QSettings
import logging
import os
import time

from .picture_view import PictureView
from .thumbnail_view import ThumbnailViewWidget
from .hotkey_manager import HotkeyManager
from .inspector_view import InspectorView
from .filter_dialog import FilterDialog
from .status_bar import CustomStatusBar
from scripts.script_manager import ScriptManager, ScriptAPI
from core.event_system import event_system, EventType, InspectorEventData, MouseEventData, KeyEventData, ViewEventData, EventData, StatusMessageEventData, StatusSection
from core.selection import SelectionState, SelectionProcessor, SelectionHistory
from network.socket_client import ThumbnailSocketClient
from network.gui_server import GuiServer

class MainWindow(QMainWindow):
    _hover_rating_ready = Signal(str, int)  # (path, rating)

    def __init__(self, config_manager, socket_client: ThumbnailSocketClient):
        super().__init__()
        self.config_manager = config_manager
        self.socket_client = socket_client

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self._layout = QVBoxLayout(self.central_widget)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self.stacked_widget = QStackedWidget()
        self._layout.addWidget(self.stacked_widget)

        self.status_bar = CustomStatusBar(self.config_manager, self)
        self.setStatusBar(self.status_bar)

        self.thumbnail_view = None
        self.picture_view = None
        self.current_hovered_image = None
        self.inspector_views: List[InspectorView] = []
        self._inspector_slot = 0

        self._setup_thumbnail_view()

        self.last_known_directory = None

        self.setWindowTitle("Hey, RabbitViewer!")
        settings = QSettings("RabbitViewer", "MainWindow")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(800, 600)

        self.filter_dialog = None
        self._removed_images = []

        QTimer.singleShot(0, self._deferred_init)

    def _deferred_init(self):
        """Heavy initialisation deferred until after the first frame is painted."""
        self.selection_state = SelectionState()
        self.selection_processor = SelectionProcessor(self.selection_state)
        self.selection_history = SelectionHistory(self.selection_processor)

        self._gui_server = GuiServer(self)
        self._gui_server.start()

        self.script_manager = ScriptManager(self)
        self.script_api = ScriptAPI(self)
        scripts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
        self.script_manager.load_scripts(scripts_dir)

        self._setup_hotkeys()
        self._setup_event_subscriptions()

    def _setup_thumbnail_view(self):
        self.thumbnail_view = ThumbnailViewWidget(self.config_manager)
        self.thumbnail_view.set_socket_client(self.socket_client)
        self.thumbnail_view.doubleClicked.connect(self._handle_thumbnail_double_click)
        self.thumbnail_view.benchmarkComplete.connect(self._handle_benchmark_result)
        self.stacked_widget.addWidget(self.thumbnail_view)

        self._hover_prefetch_path: Optional[str] = None
        self._hover_prefetch_timer = QTimer(self)
        self._hover_prefetch_timer.setSingleShot(True)
        self._hover_prefetch_timer.setInterval(150)
        self._hover_prefetch_timer.timeout.connect(self._do_hover_prefetch)
        self._hover_clear_timer = QTimer(self)
        self._hover_clear_timer.setSingleShot(True)
        self._hover_clear_timer.setInterval(100)
        self._hover_clear_timer.timeout.connect(self._do_hover_clear)
        self.thumbnail_view.thumbnailHovered.connect(self._on_thumbnail_hovered)
        self.thumbnail_view.thumbnailLeft.connect(self._on_thumbnail_left)
        self.thumbnail_view.filtersApplied.connect(self._on_filters_applied)
        self._hover_rating_ready.connect(self._on_hover_rating_ready)

    def _handle_benchmark_result(self, operation: str, time: float):
        logging.info(f"Benchmark - {operation}: {time:.3f} seconds")

    def _on_thumbnail_hovered(self, path: str):
        if self.stacked_widget.currentWidget() is self.picture_view:
            return
        # Cancel any pending clear — cursor moved to another thumbnail
        self._hover_clear_timer.stop()
        # Publish filepath immediately — no network needed
        if path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="main_window",
                timestamp=time.time(),
                message=path,
                section=StatusSection.FILEPATH,
            ))
        self._hover_prefetch_path = path
        self._hover_prefetch_timer.start()

    def _on_thumbnail_left(self):
        if self.stacked_widget.currentWidget() is self.picture_view:
            return
        # Defer clear: if cursor enters another thumbnail within 100 ms the
        # timer is cancelled in _on_thumbnail_hovered, avoiding flicker.
        self._hover_clear_timer.start()

    def _do_hover_clear(self):
        if self.status_bar:
            self.status_bar.setFilepath("")
            self.status_bar.clearRating()

    def _do_hover_prefetch(self):
        path = self._hover_prefetch_path
        if path:
            self._prefetch_view_image_async(path)
            threading.Thread(
                target=self._fetch_hover_rating, args=(path,), daemon=True
            ).start()

    def _fetch_hover_rating(self, path: str):
        if not self.socket_client:
            return
        try:
            resp = self.socket_client.get_metadata_batch([path])
            rating = 0
            if resp and path in resp.metadata:
                rating = resp.metadata[path].get("rating", 0) or 0
            self._hover_rating_ready.emit(path, int(rating))
        except Exception as e:
            logging.debug(f"Hover rating fetch failed for {path}: {e}")

    def _on_hover_rating_ready(self, path: str, rating: int):
        if self.thumbnail_view.get_hovered_image_path() == path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="main_window",
                timestamp=time.time(),
                message=str(rating),
                section=StatusSection.RATING,
            ))

    def _prefetch_view_image_async(self, path: str):
        if not self.socket_client or not path:
            return
        threading.Thread(
            target=self.socket_client.request_view_image,
            args=(path,),
            daemon=True,
        ).start()

    def _prefetch_neighbors(self, image_path: str):
        files = self.thumbnail_view.current_files
        if not files:
            return
        try:
            idx = files.index(image_path)
        except ValueError:
            return
        n = len(files)
        for neighbor_idx in {(idx - 1) % n, (idx + 1) % n} - {idx}:
            self._prefetch_view_image_async(files[neighbor_idx])

    def _open_inspector_window(self):
        """Create and show a new inspector window."""
        inspector = InspectorView(self.config_manager, inspector_index=self._inspector_slot)
        self._inspector_slot += 1
        inspector.set_socket_client(self.socket_client)
        self.inspector_views.append(inspector)
        inspector.closed.connect(lambda: self._on_inspector_closed(inspector))
        inspector.show()
        if self.picture_view and self.stacked_widget.currentWidget() == self.picture_view:
            self._force_inspector_update_from_picture_view()
        logging.info("Opened new Inspector window.")

    def _on_inspector_closed(self, inspector):
        try:
            self.inspector_views.remove(inspector)
        except ValueError:
            return  # already removed by closeEvent teardown loop
        if not self.inspector_views:
            self._inspector_slot = 0

    def open_filter_dialog(self):
        """Create and show the filter dialog."""
        if not self.filter_dialog:
            self.filter_dialog = FilterDialog(self)
            self.filter_dialog.filter_changed.connect(self._handle_filter_changed)
            self.filter_dialog.stars_changed.connect(self._handle_stars_changed)
            
        if self.filter_dialog.isVisible():
            self.filter_dialog.hide()
            # Clear filter when dialog is closed
            if self.thumbnail_view:
                self.thumbnail_view.clear_filter()
        else:
            self.filter_dialog.show()
            self.filter_dialog.raise_()
            self.filter_dialog.activateWindow()
            
    def _handle_filter_changed(self, filter_text: str):
        """Handle filter text changes from the filter dialog."""
        logging.debug(f"Filter changed: {filter_text}")
        if self.thumbnail_view:
            self.thumbnail_view.apply_filter(filter_text)
        else:
            logging.warning("Filter changed but no thumbnail_view available")
            
    def _handle_stars_changed(self, star_states: list):
        """Handle star filter changes from the filter dialog."""
        logging.debug(f"Stars changed: {star_states}")
        if self.thumbnail_view:
            self.thumbnail_view.apply_star_filter(star_states)
        else:
            logging.warning("Stars changed but no thumbnail_view available")

    def _on_filters_applied(self):
        """After filter re-applies, refresh UI state for the currently active image."""
        # Case 1: picture_view is open — navigate away if its current image is now filtered out
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            current_path = self.picture_view.current_path
            visible = set(self.thumbnail_view.current_files)  # O(1) lookup
            if current_path and current_path not in visible:
                if visible:
                    first = self.thumbnail_view.current_files[0]
                    self.picture_view.loadImage(first)
                    self._prefetch_neighbors(first)
                else:
                    # All images filtered out — return to thumbnail grid rather than
                    # leaving picture_view stranded showing a now-hidden image.
                    self.stacked_widget.setCurrentWidget(self.thumbnail_view)
            return

        # Case 2: thumbnail_view is active — re-emit hover so inspector/status bar refresh
        hovered_path = self.thumbnail_view.get_hovered_image_path()
        if hovered_path:
            self.thumbnail_view.thumbnailHovered.emit(hovered_path)
            
    def _force_inspector_update_from_picture_view(self):
        """Force an inspector update from the current picture view state."""
        if (self.picture_view and self.picture_view.current_path and
                self.picture_view._picture_base.has_image()):
            try:
                # Use center of the view as initial position
                center_pos = QPointF(0.5, 0.5)
                
                event_data = InspectorEventData(
                    event_type=EventType.INSPECTOR_UPDATE,
                    source="main_window",
                    timestamp=time.time(),
                    image_path=self.picture_view.current_path,
                    normalized_position=center_pos
                )
                event_system.publish(event_data)
            except Exception as e:  # why: publish invokes arbitrary subscriber callbacks
                logging.error(f"Error forcing inspector update: {e}", exc_info=True)
            
    def closeEvent(self, event):
        """Handles the window close event."""
        logging.info("GUI close requested.")
        self._hover_clear_timer.stop()
        self._hover_prefetch_timer.stop()

        if hasattr(self, '_gui_server'):
            self._gui_server.stop()

        # Close any other windows like inspectors
        for inspector in list(self.inspector_views):
            inspector.close()
        self.inspector_views.clear()  # safety net: closed signal may not fire for all inspectors
        settings = QSettings("RabbitViewer", "MainWindow")
        settings.setValue("geometry", self.saveGeometry())
        settings.sync()
        event.accept()
        QApplication.instance().quit()

    def _setup_event_subscriptions(self):
        """Subscribe to inspector events to track hovered image."""
        event_system.subscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_event)
        # Subscribe to undo/redo events, which might be triggered by menus/etc.
        event_system.subscribe(EventType.UNDO_SELECTION, lambda data: self.selection_history.undo())
        event_system.subscribe(EventType.REDO_SELECTION, lambda data: self.selection_history.redo())
        event_system.subscribe(EventType.STATUS_MESSAGE, self._handle_status_message)
        event_system.subscribe(EventType.OPEN_FILTER, lambda _: self.open_filter_dialog())

    def _handle_status_message(self, event_data: StatusMessageEventData):
        """Route a status message to the appropriate section."""
        if not self.status_bar:
            return
        if event_data.section == StatusSection.FILEPATH:
            self.status_bar.setFilepath(event_data.message)
        elif event_data.section == StatusSection.RATING:
            val = int(event_data.message) if event_data.message.isdigit() else None
            self.status_bar.setRating(val)
        else:
            self.status_bar.setProcessMessage(event_data.message, event_data.timeout)

    def _handle_inspector_event(self, event_data):
        """Handle inspector events to track the currently hovered image."""
        self.current_hovered_image = event_data.image_path
        logging.debug(f"Hovered image updated: {self.current_hovered_image}")

    def _setup_hotkeys(self):
        """Initialize hotkey manager with unified configuration"""
        hotkeys_config = self.config_manager.get("hotkeys", {})
        self.hotkey_manager = HotkeyManager(self, hotkeys_config)
        
        self.hotkey_manager.add_action("toggle_inspector", self._open_inspector_window)
        self.hotkey_manager.add_action("escape_picture_view", self.close_picture_view)
        self.hotkey_manager.add_action("close_or_quit", self._handle_close_or_quit)
        self.hotkey_manager.add_action("next_image", lambda: self.navigate_to_image("next"))
        self.hotkey_manager.add_action("previous_image", lambda: self.navigate_to_image("previous"))
        self.hotkey_manager.add_action("undo_selection", self.selection_history.undo)
        self.hotkey_manager.add_action("redo_selection", self.selection_history.redo)
        
    def load_directory(self, directory_path: str, recursive: bool = True):
        """Load a directory of images into the thumbnail view."""
        logging.info(f"MainWindow: Starting to load directory: {directory_path} (Recursive: {recursive})")
        self.last_known_directory = directory_path
        logging.info("MainWindow: Calling thumbnail_view.load_directory...")
        self.thumbnail_view.load_directory(directory_path, recursive)
        logging.info("MainWindow: Directory loading completed, setting current widget...")
        self.stacked_widget.setCurrentWidget(self.thumbnail_view)
        logging.info("MainWindow: ThumbnailView is now the current widget")

    def get_removed_images(self) -> Set[str]:
        return set(self._removed_images)

    def remove_images(self, image_paths: List[str]):
        """Remove images from the thumbnail view"""
        if self.thumbnail_view:
            self.thumbnail_view.remove_images(image_paths)
            self._removed_images = image_paths.copy() 

        
    @Slot()
    def _handle_thumbnail_double_click(self):
        """Handle double-click on thumbnail by opening the currently hovered image."""
        target_image = self.current_hovered_image
        if not target_image:
            return
        self._open_picture_view(target_image)
        
    def _open_picture_view(self, image_path: str):
        if not os.path.exists(image_path):
            logging.error(f"Original file does not exist: {image_path}")
            return
        try:
            if not self.picture_view:
                self.picture_view = PictureView()
                self.picture_view.escapePressed.connect(self.close_picture_view)
                self.picture_view.set_socket_client(self.socket_client)
                self.stacked_widget.addWidget(self.picture_view)
            self.picture_view.loadImage(image_path)
            self._prefetch_neighbors(image_path)
            self._hover_clear_timer.stop()
            self.stacked_widget.setCurrentWidget(self.picture_view)
            self.picture_view.setFocus()
        except Exception as e:  # why: loadImage delegates to format plugins which may raise arbitrarily
            logging.error(f"Exception when opening Picture View: {e}", exc_info=True)
            
    def _handle_close_or_quit(self):
        """Cascade: close picture view → close last inspector → quit."""
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            self.close_picture_view()
        elif self.inspector_views:
            self.inspector_views[-1].close()
        else:
            self.close()

    def close_picture_view(self):
        """Close picture view and return to thumbnail view."""
        logging.debug("Closing picture view")
        try:
            if self.picture_view:
                current_path = self.picture_view.current_path
                self.stacked_widget.setCurrentWidget(self.thumbnail_view)

                # Highlight the last viewed image in thumbnail view
                if current_path:
                    self.thumbnail_view.setHighlightedThumbnail(current_path)

                # close() triggers closeEvent (unsubscribes events) then deleteLater() via WA_DeleteOnClose.
                self.picture_view.close()
                self.picture_view = None
        except RuntimeError as e:
            logging.error(f"Error closing picture view: {e}", exc_info=True)

    def navigate_to_image(self, direction: str):
        """Navigate to next/previous image in picture view using visible images only."""
        if not self.picture_view or not self.picture_view.current_path:
            return
            
        try:
            current_path = self.picture_view.current_path
            try:
                current_idx = self.thumbnail_view.current_files.index(current_path)
            except ValueError:
                logging.warning(f"Current image {current_path} not found in visible files")
                return
            num_visible = len(self.thumbnail_view.current_files)
            if num_visible == 0:
                return
            if direction == "next":
                new_idx = (current_idx + 1) % num_visible
            elif direction == "previous":
                new_idx = (current_idx - 1 + num_visible) % num_visible
            else:
                return
            new_path = self.thumbnail_view.current_files[new_idx]
            self.picture_view.loadImage(new_path)
            self._prefetch_neighbors(new_path)
        except Exception as e:  # why: loadImage delegates to format plugins which may raise arbitrarily
            logging.error(f"Error navigating to {direction} image: {e}", exc_info=True)
