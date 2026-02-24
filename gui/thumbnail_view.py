from __future__ import annotations
import os
import time
import logging
from math import floor
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Set
from PySide6.QtCore import (
    Qt, Signal, QTimer, QElapsedTimer, QPoint, QPointF, QRectF, QSizeF, QEvent, QRect, QSize, Slot
)
from PySide6.QtGui import QPixmap, QImage, QColor, QMouseEvent, QKeyEvent, QCursor
from PySide6.QtWidgets import (
    QLabel, QVBoxLayout, QScrollArea, QGridLayout, QWidget, QFrame, QMainWindow, QApplication, QHBoxLayout
)

from network.socket_client import ThumbnailSocketClient
from network import protocol
_ValidationErrors = (ValueError, TypeError, KeyError)
from gui.components.grid_layout_manager import GridLayoutManager
from utils.thumbnail_filters import matches_filter
from core.selection import ReplaceSelectionCommand, AddToSelectionCommand, ToggleSelectionCommand, RemoveFromSelectionCommand
from core.event_system import event_system, EventType, InspectorEventData, SelectionChangedEventData, DaemonNotificationEventData, StatusMessageEventData, EventData
from core.heatmap import compute_heatmap, THUMB_RING_COUNT

from dataclasses import dataclass

@dataclass
class ImageState:
    loaded: bool = False
    prioritized: bool = False
    matches_filter: bool = True # Does it match the current filter criteria?

class ThumbnailLabel(QLabel):

    def __init__(self, file_path: str, size: int, config: dict):
        super().__init__()
        self.file_path = file_path
        self.original_path = file_path
        self.size = size
        self.loaded = False
        self.selected = False
        self.config = config

        self._original_idx: int = -1
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(self._makeStyleSheet())
        self.setMouseTracking(True)

        # Throttle inspector events to ~60 fps so rapid mouse movement does not
        # flood the event system and block the GUI thread with socket calls.
        self._pending_norm_pos: Optional[QPointF] = None
        self._inspector_timer = QTimer(self)
        self._inspector_timer.setSingleShot(True)
        self._inspector_timer.setInterval(16)  # ~60 fps
        self._inspector_timer.timeout.connect(self._flushInspectorEvent)

    def _makeStyleSheet(self) -> str:
        border_width = self.config.get("border_width", 1)
        border_color = self.config.get(
            "select_border_color",
            "orange") if self.selected else "transparent"
        return f"""
            QLabel {{
                background-color: {self.config.get("placeholder_color", "#1a1a1a")};
                border: {border_width}px solid {border_color};
            }}
            QLabel:hover {{
                border: {border_width}px solid {self.config.get("hover_border_color", "#2d59b6")};
            }}
        """

    def updateThumbnail(self, pixmap: QPixmap):
        if not pixmap.isNull():
            # Don't upscale: only scale down if the pixmap exceeds the label size.
            if pixmap.width() > self.size or pixmap.height() > self.size:
                scaled = pixmap.scaled(
                    self.size,
                    self.size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation)
            else:
                scaled = pixmap
            self.setPixmap(scaled)
            self.loaded = True

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Ignore the event so it propagates to the parent widget for handling.
            event.ignore()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._queueInspectorEvent(event.position())
        super().mouseMoveEvent(event)

    def _queueInspectorEvent(self, pos: QPointF):
        """Coalesce rapid mouse-move events; publish at most once per 16 ms."""
        try:
            widget_rect = self.rect()
            if widget_rect.width() > 0 and widget_rect.height() > 0:
                norm_x = max(0.0, min(1.0, pos.x() / widget_rect.width()))
                # Invert Y: Qt has (0,0) at top-left, we want (0,0) at bottom-left
                norm_y = max(0.0, min(1.0, 1.0 - (pos.y() / widget_rect.height())))
                self._pending_norm_pos = QPointF(norm_x, norm_y)
                if not self._inspector_timer.isActive():
                    self._inspector_timer.start()
        except (AttributeError, TypeError) as e:
            # why: rect() can return garbage dimensions during widget teardown if a
            # mouse event fires after hide() but before deletion.
            logging.error("Error queuing inspector event from thumbnail: %s", e, exc_info=True)

    def _flushInspectorEvent(self):
        pos = self._pending_norm_pos
        if pos is None:
            return
        self._pending_norm_pos = None
        event_data = InspectorEventData(
            event_type=EventType.INSPECTOR_UPDATE,
            source="thumbnail_view",
            timestamp=time.time(),
            image_path=self.original_path,
            normalized_position=pos,
        )
        event_system.publish(event_data)

    def setSelected(self, selected: bool):
        if self.selected != selected:
            self.selected = selected
            self.setStyleSheet(self._makeStyleSheet())


class ThumbnailViewWidget(QFrame):
    doubleClicked = Signal(str)
    thumbnailHovered = Signal(str)  # emits original_path on Enter
    thumbnailLeft = Signal()         # emits when hover ends (no path)
    benchmarkComplete = Signal(str, float)
    filtersApplied = Signal()
    initialScanReady = Signal()
    _thumbnail_generated_signal = Signal(str, QImage, object)
    _daemon_notification_received = Signal(object)
    # Dedicated signal for the DB-response file list so it always triggers an
    # immediate layout update, regardless of what fast-scan batches arrived first.
    _initial_files_signal = Signal(list)
    _initial_thumbs_signal = Signal(dict)
    _filtered_paths_ready = Signal(object)  # set of visible paths from daemon

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.config_manager = config_manager
        self.gui_config = config_manager.get(
            "gui", {}) if config_manager else {}
        self.display_size = int(config_manager.get("thumbnail_size", 128))
        self.cache_dir = os.path.expanduser(config_manager.get("cache_dir"))
        self.spacing = self.gui_config.get("spacing", 5)
        self.socket_client: Optional[ThumbnailSocketClient] = None
        self.current_directory_path: Optional[str] = None

        # Initialize grid layout manager
        self._grid_layout_manager = None

        self.labels: Dict[int, ThumbnailLabel] = {}
        self.pending_thumbnails = set()
        self.ready_thumbnails: Dict[str, QPixmap] = {}
        self._initial_thumb_paths: Dict[str, str] = {}  # {source_path: local_thumbnail_path}
        self.current_files = []
        self.all_files = []
        self._all_files_set: Set[str] = set()
        self._path_to_idx: Dict[str, int] = {}
        self.middle_mouse_pressed = False
        self.middle_mouse_press_pos = None
        self._benchmark_timer = QElapsedTimer()
        self._last_load_time = 0
        self._last_redraw_time = 0

        # State for selection UI logic
        self.selection_anchor_index: Optional[int] = None
        self.hotkey_range_selection_active = False

        # State for refined click-and-drag selection
        self._selection_mode: Optional[str] = None
        self._drag_start_index: int = -1
        self._drag_last_index: int = -1
        self._current_selection: Set[str] = set()
        self._selected_indices: Set[int] = set()
        self._last_preview_selected: Set[int] = set()

        self._setupUI()
        self.viewport().installEventFilter(self)
        self.installEventFilter(self)
        self._setupResizeTimer()
        self.setMouseTracking(True)
        self._grid_container.setMouseTracking(True)
        self.scroll_area.setMouseTracking(True)
        self.scroll_area.viewport().setMouseTracking(True)

        self._initializeLayout()
        self._widget_pool = []
        self._pool_size = 100
        self._chunk_size = 100
        self._thumbnail_cache = {}
        self._cache_size = 5000

        self._last_resize_size = self.size()

        # Filter state
        self._current_filter = ""
        self._current_star_filter = [True, True, True, True, True, True]
        self._hidden_indices = set()
        self._visible_to_original_mapping = {}
        self._original_to_visible_mapping = {}
        self._visible_original_indices: List[int] = []
        self._last_layout_file_count = 0
        self._last_thumb_pairs: dict[str, int] = {}   # path → priority from last heatmap
        self._last_fullres_pairs: dict[str, int] = {}  # path → priority from last heatmap
        self._viewport_generation: int = 0  # monotonic counter; stale IPC calls check this

        self.image_states: Dict[int, ImageState] = {}

        # Startup timing — reset in load_directory, logged at each pipeline milestone.
        self._startup_t0: Optional[float] = None
        self._startup_first_scan_progress: bool = False
        self._needs_heatmap_seed: bool = False
        self._startup_first_previews_ready: bool = False
        self._startup_first_inline_thumb: bool = False
        self._startup_inline_thumb_count: int = 0

        self._filter_update_timer = QTimer(self)
        self._filter_update_timer.setSingleShot(True)
        self._filter_update_timer.setInterval(200)
        self._filter_update_timer.timeout.connect(self.reapply_filters)

        # Buffer for incoming previews_ready notifications.  Instead of loading
        # each QImage immediately on the main thread (which floods the event loop
        # and causes all thumbnails to appear in one big batch), we queue paths
        # here and drain them ~60fps via _preview_tick_timer.  This gives Qt time
        # to repaint between batches so thumbnails appear progressively.
        self._pending_previews: list = []  # [(image_path, thumbnail_path), ...]
        self._preview_tick_timer = QTimer(self)
        self._preview_tick_timer.setInterval(16)  # ~60 fps drain rate
        self._preview_tick_timer.timeout.connect(self._tick_preview_loading)

        # Chunked label creation: instead of creating all labels synchronously
        # in _add_image_batch (which freezes the GUI for large directories), we
        # buffer (file_path, original_idx) pairs and drain them at ~60fps.
        self._pending_labels: list = []  # [(file_path, original_idx), ...]
        self._label_tick_timer = QTimer(self)
        self._label_tick_timer.setInterval(16)  # ~60 fps, same as preview timer
        self._label_tick_timer.timeout.connect(self._tick_label_creation)

        # Fires periodically while scrolling so thumbnails update continuously,
        # not just after scrolling stops.  Stopped when idle (no scroll for one
        # full interval) to avoid unnecessary heatmap recomputation.
        self._priority_update_timer = QTimer(self)
        self._priority_update_timer.setInterval(150)
        self._priority_update_timer.timeout.connect(self._prioritize_visible_thumbnails)
        self._scroll_idle_timer = QTimer(self)
        self._scroll_idle_timer.setSingleShot(True)
        self._scroll_idle_timer.setInterval(200)
        self._scroll_idle_timer.timeout.connect(self._on_scroll_idle)

        self._viewport_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viewport")

        event_system.subscribe(EventType.SELECTION_CHANGED, self._on_selection_changed)
        event_system.subscribe(EventType.RANGE_SELECTION_START, lambda _: self.start_range_selection())
        event_system.subscribe(EventType.RANGE_SELECTION_END, lambda _: self.end_range_selection())
        event_system.subscribe(EventType.DAEMON_NOTIFICATION, self._handle_daemon_notification_from_thread)
        self._daemon_notification_received.connect(self._process_daemon_notification)
        self._initial_files_signal.connect(self._on_initial_files_received)
        self._initial_thumbs_signal.connect(self._on_initial_thumbs_received)
        self._filtered_paths_ready.connect(self._on_filtered_paths_ready)

        self._hovered_label: Optional[ThumbnailLabel] = None
        self._thumbnail_generated_signal.connect(self._on_thumbnail_ready, Qt.QueuedConnection)

        self._is_loading = False
        self._folder_is_cached = False
        self._filter_in_flight = False
        self._filter_pending = False


    def _initializeLayout(self):
        """Initialize layout calculations immediately after UI setup"""
        if self._grid_layout_manager:
            self._grid_layout_manager.initialize_layout()

    def set_socket_client(self, socket_client: ThumbnailSocketClient):
        """Set the socket client for communication with the daemon."""
        self.socket_client = socket_client

    def _set_hovered_label(self, label: ThumbnailLabel):
        if self._hovered_label != label:
            self._hovered_label = label
            self.thumbnailHovered.emit(label.original_path)
            self._priority_update_timer.start()

    def _clear_hovered_label(self, label: ThumbnailLabel):
        if self._hovered_label == label:
            self._hovered_label = None
            self.thumbnailLeft.emit()
            self._priority_update_timer.start()

    def get_hovered_image_path(self) -> Optional[str]:
        """
        Returns the path of the currently hovered image.
        """
        if self._hovered_label:
            return self._hovered_label.original_path
        return None

    def mouseMoveEvent(self, event):
        """Handle mouse movement for selection and other features."""
        super().mouseMoveEvent(event)

        if self.middle_mouse_pressed:
            delta = event.pos() - self.middle_mouse_press_pos
            h_bar = self.scroll_area.horizontalScrollBar()
            v_bar = self.scroll_area.verticalScrollBar()

            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())

            self.middle_mouse_press_pos = event.pos()

        if self.hotkey_range_selection_active and self.selection_anchor_index is not None:
            current_idx = self._get_thumbnail_at_pos(event.pos())
            self._update_selection_preview(self.selection_anchor_index, current_idx)
        elif self._drag_start_index != -1 and event.buttons() & Qt.LeftButton:
            end_index = self._get_thumbnail_at_pos(event.pos())
            # If cursor is in a gap, use the last known valid index
            if end_index is not None:
                self._drag_last_index = end_index

            self._update_selection_preview(self._drag_start_index, self._drag_last_index)

    def _recycle_label(self, label: ThumbnailLabel):
        if len(self._widget_pool) < self._pool_size:
            # Remove from layout but keep parent
            self._grid_layout.removeWidget(label)
            label.hide()
            # Don't clear pixmap if it's in cache
            if label.original_path not in self._thumbnail_cache:
                label.setPixmap(QPixmap())
            label.loaded = False
            label.selected = False
            label._original_idx = -1
            # why: cancel any pending inspector-throttle tick so the recycled label
            # cannot emit a stale INSPECTOR_UPDATE after being reassigned a new path.
            label._inspector_timer.stop()
            label._pending_norm_pos = None
            label.setParent(self._grid_container)
            self._widget_pool.append(label)
        else:
            label.deleteLater()

    def _get_or_create_label(self, file_path: str, original_idx: int) -> ThumbnailLabel:
        if original_idx in self.labels:
            label = self.labels[original_idx]
            label.file_path = file_path
            label.original_path = file_path
            label.loaded = False
            label.show()
            return label

        if self._widget_pool:
            label = self._widget_pool.pop()
            label.file_path = file_path
            label.original_path = file_path
            label.loaded = False
            label.show()
        else:
            label = ThumbnailLabel(file_path, self.display_size, self.gui_config)

        label._original_idx = original_idx
        label.setParent(self._grid_container)
        # why: ThumbnailViewWidget must be the event filter so Enter/Leave events
        # reach _set_hovered_label / _clear_hovered_label on the parent widget.
        label.installEventFilter(self)
        return label

    def eventFilter(self, obj, event):
        if obj == self.viewport():
            if event.type() == QEvent.Type.MouseButtonRelease:
                mouse_event = QMouseEvent(event)
                if mouse_event.button() == Qt.MiddleButton:
                    self.middle_mouse_pressed = False
                    self.middle_mouse_press_pos = None
                    self.viewport().setCursor(Qt.ArrowCursor)
                    return True  # Stop further processing of this event
            return False
        elif obj == self.scroll_area.viewport():
            if event.type() == QEvent.Type.MouseButtonDblClick:
                mouse_event = QMouseEvent(event)
                if mouse_event.button() == Qt.LeftButton:
                    hovered_path = self.get_hovered_image_path()
                    if hovered_path:
                        self.doubleClicked.emit(hovered_path)
                    return True  # Event handled
            return False
        elif obj == self:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                mouse_event = QMouseEvent(event)
                if mouse_event.button() == Qt.LeftButton:
                    hovered_path = self.get_hovered_image_path()
                    if hovered_path:
                        self.doubleClicked.emit(hovered_path)
                    return True  # Event handled
            return False
        elif isinstance(obj, ThumbnailLabel):
            if event.type() == QEvent.Type.Enter:
                self._set_hovered_label(obj)
                # Emit an initial inspector event so the inspector view updates
                # immediately on hover, even if the mouse doesn't move further.
                obj._queueInspectorEvent(QPointF(obj.rect().center()))
            elif event.type() == QEvent.Type.Leave:
                self._clear_hovered_label(obj)
            return False # Important: Forward event so Label can also process it
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            start_index = self._get_thumbnail_at_pos(event.pos())
            if start_index is None:
                # Click was on the background, clear selection
                cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
                event_system.publish(cmd)
                super().mousePressEvent(event)
                return

            # A thumbnail was clicked, begin the selection process
            self._drag_start_index = start_index
            self._drag_last_index = start_index
            modifiers = event.modifiers()

            if modifiers & Qt.ShiftModifier and modifiers & Qt.ControlModifier:
                self._selection_mode = "replace"
            elif modifiers & Qt.ControlModifier:
                self._selection_mode = "remove"
            elif modifiers & Qt.ShiftModifier:
                self._selection_mode = "add"
            else:
                self._selection_mode = "replace"

            self._update_selection_preview(start_index, start_index)

        super().mousePressEvent(event)

    def start_range_selection(self):
        """Starts range selection mode, usually via a hotkey."""
        # This logic is now a toggle, which is more intuitive for a key press
        if not self.hotkey_range_selection_active:
            # Start selection from the currently hovered label, which is more reliable
            if self._hovered_label is None:
                logging.warning("Cannot start range selection with hotkey; no thumbnail is hovered.")
                return

            start_idx = self._label_to_original_idx(self._hovered_label)
            if start_idx is None:
                logging.warning("Could not determine index of hovered label.")
                return

            self.hotkey_range_selection_active = True
            self.selection_anchor_index = start_idx
            self.setCursor(Qt.CrossCursor)
            # Lock in "add" mode for hotkey selection
            self._selection_mode = "add"
            self._update_selection_preview(start_idx, start_idx)
        else:
            # On second press, commit the selection
            self.end_range_selection()

    def end_range_selection(self):
        """Ends the hotkey-driven range selection mode."""
        logging.debug("Ending range selection")
        if self.hotkey_range_selection_active:
            self.hotkey_range_selection_active = False
            self.setCursor(Qt.ArrowCursor)
            # Commit the selection
            current_pos = self.mapFromGlobal(QCursor.pos())
            end_idx = self._get_thumbnail_at_pos(current_pos)
            self._commit_selection(self.selection_anchor_index, end_idx)
            self.selection_anchor_index = None
            self._selection_mode = None

    def _on_selection_changed(self, event_data: SelectionChangedEventData):
        """
        Update the visual state of changed labels when the central selection
        state changes.  Only labels whose selected state actually differs from
        the previous frame are touched (delta update).
        """
        if event_data.event_type == EventType.SELECTION_CHANGED:
            selected_paths = event_data.selected_paths
            new_indices = {self._path_to_idx[p] for p in selected_paths if p in self._path_to_idx}
            newly_selected = new_indices - self._selected_indices
            deselected = self._selected_indices - new_indices
            for idx in newly_selected:
                label = self.labels.get(idx)
                if label:
                    label.setSelected(True)
            for idx in deselected:
                label = self.labels.get(idx)
                if label:
                    label.setSelected(False)
            self._selected_indices = new_indices
            self._current_selection = selected_paths

    def _label_to_original_idx(self, label: ThumbnailLabel) -> Optional[int]:
        idx = label._original_idx
        return idx if idx >= 0 else None

    def viewport(self):
        return self.scroll_area.viewport()

    def _setupUI(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setSpacing(0)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._viewport_widget = QWidget()
        v_layout = QVBoxLayout(self._viewport_widget)
        h_layout = QHBoxLayout()

        v_layout.addStretch(1)
        v_layout.addLayout(h_layout)
        v_layout.addStretch(1)
        h_layout.addStretch(1)

        self._grid_container = QWidget()
        self._grid_container.setContentsMargins(0, 0, 0, 0)

        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(self.spacing)
        self._grid_layout.setContentsMargins(self.spacing, self.spacing, self.spacing, self.spacing)

        h_layout.addWidget(self._grid_container)
        h_layout.addStretch(1)

        self._grid_layout_manager = GridLayoutManager(
            self._grid_layout,
            self._grid_container,
            self.scroll_area,
            self.display_size,
            self.spacing
        )
        self.scroll_area.setWidget(self._viewport_widget)
        # Install event filter on scroll area to handle double clicks correctly
        self.scroll_area.viewport().installEventFilter(self)

        self.main_layout.addWidget(self.scroll_area)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _setupResizeTimer(self):
        """Setup timer for delayed layout updates during resize"""
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._performDelayedLayoutUpdate)
        self._resize_timer.setInterval(150)  # 150ms delay for better performance

    def resizeEvent(self, event):
        super().resizeEvent(event)
        size_diff = abs(event.size().width() - self._last_resize_size.width())
        if size_diff < 50:  # Skip small resize events
            return
        self._last_resize_size = event.size()
        self._resize_timer.start()

    def _performDelayedLayoutUpdate(self):
        """Perform layout update after resize timer expires"""
        if self._grid_layout_manager:
            self._grid_layout_manager.update_layout()
        self._priority_update_timer.start()

    def load_directory(self, directory_path: str, recursive: bool = True):
        """Clears the view and starts the asynchronous directory loading process."""
        self._startup_t0 = time.perf_counter()
        self._startup_first_scan_progress = False
        self._startup_first_previews_ready = False
        self._startup_first_inline_thumb = False
        self._startup_inline_thumb_count = 0
        logging.info(f"[startup] load_directory called for {directory_path}")
        self.clear_layout()
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="thumbnail_view",
            timestamp=time.time(),
            message=f"Scanning {os.path.basename(directory_path)}...",
            timeout=0 # a timeout of 0 makes it persistent until the next message
        ))
        self._is_loading = True
        self._folder_is_cached = False
        self.current_directory_path = directory_path
        self._load_directory_deferred(directory_path, recursive)

    def _load_directory_deferred(self, directory_path: str, recursive: bool = True):
        """Starts a background thread to get files from the daemon without blocking the GUI."""
        logging.info(f"Querying daemon for files in: {directory_path} (Recursive: {recursive})")
        thread = threading.Thread(target=self._get_files_from_daemon, args=(directory_path, recursive), daemon=True)
        thread.start()

    def _get_files_from_daemon(self, directory_path: str, recursive: bool = True):
        """Runs in a thread to fetch the file list from the daemon's database."""
        response = self.socket_client.get_directory_files(directory_path, recursive)
        if response and response.status == "success":
            logging.info(f"Daemon acknowledged scan request for {directory_path}. Waiting for progress notifications.")
            # Emit via the dedicated signal so the DB-response batch always shows
            # placeholders immediately, even if fast-scan notifications arrived first
            # and consumed the is_first_batch shortcut in _add_image_batch.
            self._initial_files_signal.emit(sorted(response.files))
            # Feed cached thumbnail paths directly into the preview pipeline
            # so the GUI loads QImages from local cache without a daemon round-trip.
            if hasattr(response, 'thumbnail_paths') and response.thumbnail_paths:
                logging.info(f"[startup] {len(response.thumbnail_paths)} cached thumbnail paths from initial response")
                self._initial_thumbs_signal.emit(response.thumbnail_paths)
        else:
            logging.error(f"Failed to request file list for {directory_path} from daemon. Response: {response}")

    @Slot(list)
    def _on_initial_files_received(self, files: list):
        """
        Handles the DB-response file list.  Creates the first chunk of labels
        synchronously so placeholders paint in the same frame, then lets the
        timer handle the rest at ~60fps.
        """
        self._folder_is_cached = len(files) > 0
        if not files:
            return
        logging.info(f"[chunking] _on_initial_files_received: {len(files)} files from DB")
        self._add_image_batch(files)
        # Drain one chunk immediately so the first screenful of placeholders
        # paints without waiting for a timer tick.
        if self._pending_labels:
            logging.info(f"[chunking] draining first chunk synchronously ({len(self._pending_labels)} pending)")
            self._tick_label_creation()
        if self.all_files:
            self.reapply_filters()

    @Slot(dict)
    def _on_initial_thumbs_received(self, thumb_map: dict):
        """Store cached thumbnail paths for use during label creation.

        Labels created by _tick_label_creation will pick these up and
        load QImages inline — no second pipeline pass needed.
        """
        self._initial_thumb_paths.update(thumb_map)

    def _handle_daemon_notification_from_thread(self, event_data: DaemonNotificationEventData):
        """
        Thread-safe method to receive notifications. Emits a signal to process on the GUI thread.
        """
        self._daemon_notification_received.emit(event_data)

    @Slot(object)
    def _process_daemon_notification(self, event_data: DaemonNotificationEventData):
        """
        Handles daemon notifications on the main GUI thread.
        """
        if event_data.notification_type == "previews_ready":
            try:
                data = protocol.PreviewsReadyData.model_validate(event_data.data)

                if not self._startup_first_previews_ready and self._startup_t0 is not None:
                    self._startup_first_previews_ready = True
                    elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                    logging.info(f"[startup] first previews_ready: {elapsed_ms:.0f} ms after load_directory")
                logging.info(f"ThumbnailViewWidget received notification: Previews ready for {data.image_path}")

                if data.thumbnail_path:
                    # Skip notifications for files not in the current directory.
                    # Daemon background work (watchdog, previous sessions) can produce
                    # previews_ready for unrelated files that waste tick slots.
                    if data.image_path not in self._path_to_idx:
                        return
                    # Buffer the path instead of loading QImage immediately.  Draining
                    # the buffer via _preview_tick_timer lets the event loop repaint
                    # between batches, producing smooth progressive thumbnail reveal
                    # rather than a single large batch.
                    self._pending_previews.append((data.image_path, data.thumbnail_path))
                    if not self._preview_tick_timer.isActive():
                        self._preview_tick_timer.start()
                else:
                    logging.debug(f"[thumb] previews_ready has no thumbnail_path for {os.path.basename(data.image_path)}")
            except _ValidationErrors as e:
                logging.error(f"Error processing 'previews_ready' notification: {e}", exc_info=True)
        elif event_data.notification_type == "scan_progress":
            try:
                # The GUI's only job is to add placeholders as they are discovered.
                data = protocol.ScanProgressData.model_validate(event_data.data)
                first_batch = not self._startup_first_scan_progress
                if first_batch and self._startup_t0 is not None:
                    self._startup_first_scan_progress = True
                    elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                    logging.info(f"[startup] first scan_progress: {elapsed_ms:.0f} ms after load_directory ({len(data.files)} files in batch)")
                logging.info(f"Received scan_progress batch for '{data.path}' with {len(data.files)} files.")
                self._add_image_batch(sorted(data.files))
                # Mark that the first layout after this batch should seed the
                # heatmap immediately.  We cannot call _prioritize_visible_thumbnails
                # here because _visible_to_original_mapping is not yet populated —
                # label creation and layout update happen asynchronously via timers.
                if first_batch:
                    self._needs_heatmap_seed = True
            except _ValidationErrors as e:
                logging.error(f"Error processing 'scan_progress' notification: {e}", exc_info=True)

        elif event_data.notification_type == "files_removed":
            try:
                data = protocol.FilesRemovedData.model_validate(event_data.data)
                if data.files:
                    logging.info(f"Removing {len(data.files)} ghost files from view.")
                    self.remove_images(data.files)
            except _ValidationErrors as e:
                logging.error(f"Error processing 'files_removed' notification: {e}", exc_info=True)

        elif event_data.notification_type == "scan_complete":
            if self._startup_t0 is not None:
                elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                logging.info(f"[startup] scan_complete: {elapsed_ms:.0f} ms after load_directory")
            logging.info(
                f"[chunking] scan_complete: all_files={len(self.all_files)}, "
                f"labels={len(self.labels)}, pending_labels={len(self._pending_labels)}, "
                f"current_files(in layout)={len(self.current_files)}, "
                f"label_timer_active={self._label_tick_timer.isActive()}"
            )
            # Stop any pending batched update, as this is the final one.
            self._filter_update_timer.stop()
            # Mark loading complete before reapply_filters() so the daemon is queried
            # with the current filter rather than showing all files unconditionally.
            self._is_loading = False
            self.reapply_filters()


    def _add_image_batch(self, files: List[str]):
        """Adds a batch of new file paths and queues label creation in chunks.

        Index bookkeeping (all_files, _all_files_set, _path_to_idx) is done
        immediately so lookups and deduplication work.  Actual label allocation
        is deferred to _tick_label_creation which drains _pending_labels at
        ~60fps, keeping the GUI responsive for large directories.
        """
        if not files:
            return

        new_files = [f for f in files if f not in self._all_files_set]
        if not new_files:
            logging.debug(f"[chunking] _add_image_batch: all {len(files)} files already known, skipping")
            return

        start_idx = len(self.all_files)
        self.all_files.extend(new_files)
        self._all_files_set.update(new_files)
        for i, f in enumerate(new_files):
            self._path_to_idx[f] = start_idx + i

        # Queue label creation for chunked processing.
        self._pending_labels.extend(
            (f, start_idx + i) for i, f in enumerate(new_files)
        )
        logging.info(
            f"[chunking] _add_image_batch: +{len(new_files)} new files "
            f"(all_files={len(self.all_files)}, pending_labels={len(self._pending_labels)}, "
            f"labels={len(self.labels)})"
        )
        if not self._label_tick_timer.isActive():
            self._label_tick_timer.start()

    _LABEL_TICK_BATCH = 500  # labels created per 16ms tick (~0.01ms each)

    def _tick_label_creation(self):
        """Drains up to _LABEL_TICK_BATCH items from _pending_labels per tick.

        Creates ImageState + ThumbnailLabel for each, applies any cached
        thumbnails, then triggers a layout update.  Stops the timer when
        the queue is empty.
        """
        batch = self._pending_labels[:self._LABEL_TICK_BATCH]
        del self._pending_labels[:self._LABEL_TICK_BATCH]

        for file_path, original_idx in batch:
            self.image_states[original_idx] = ImageState()
            label = self._get_or_create_label(file_path, original_idx)
            if label:
                if file_path in self.ready_thumbnails:
                    label.updateThumbnail(self.ready_thumbnails[file_path])
                    self.image_states[original_idx].loaded = True
                    del self.ready_thumbnails[file_path]
                elif file_path in self._initial_thumb_paths:
                    thumb_path = self._initial_thumb_paths.pop(file_path)
                    image = QImage(thumb_path)
                    if not image.isNull():
                        label.updateThumbnail(QPixmap.fromImage(image))
                        self.image_states[original_idx].loaded = True
                        self._startup_inline_thumb_count += 1
                        if not self._startup_first_inline_thumb and self._startup_t0 is not None:
                            self._startup_first_inline_thumb = True
                            elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                            logging.info(f"[startup] first inline thumbnail: {elapsed_ms:.0f} ms after load_directory")
                self.labels[original_idx] = label

        remaining = len(self._pending_labels)
        logging.info(
            f"[chunking] _tick_label_creation: created {len(batch)} labels "
            f"(labels={len(self.labels)}, pending={remaining}, "
            f"all_files={len(self.all_files)}, is_loading={self._is_loading})"
        )

        if not self._pending_labels:
            self._label_tick_timer.stop()
            if self._startup_t0 is not None and self._startup_inline_thumb_count > 0:
                elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                logging.info(
                    f"[startup] label queue drained: {self._startup_inline_thumb_count} "
                    f"inline thumbnails applied in {elapsed_ms:.0f} ms"
                )
            logging.info("[chunking] _tick_label_creation: queue drained, timer stopped")

        # Trigger layout update for the labels just created.
        self._filter_update_timer.start()

    def add_images(self, image_paths: List[str]) -> None:
        """Add images to the view, deduplicating against the current file list."""
        normalized = [os.path.abspath(p) for p in image_paths]
        self._add_image_batch(normalized)

    def remove_images(self, paths: List[str]):
        """Remove images with performance benchmarking"""
        if not paths:
            return

        self._benchmark_timer.start()

        try:
            paths_set = set(paths)
            new_all_files = []
            new_image_states = {}
            new_labels = {}

            current_new_idx = 0
            for original_idx, file_path in enumerate(self.all_files):
                if file_path not in paths_set:
                    new_all_files.append(file_path)

                    if original_idx in self.image_states:
                        new_image_states[current_new_idx] = self.image_states[original_idx]

                    if original_idx in self.labels:
                        label = self.labels[original_idx]
                        new_labels[current_new_idx] = label
                        label._original_idx = current_new_idx
                        label.file_path = file_path
                        label.original_path = file_path
                    current_new_idx += 1
                else:
                    # Recycle label if it's being removed
                    if original_idx in self.labels:
                        self._recycle_label(self.labels[original_idx])

            self.all_files = new_all_files
            self._all_files_set = set(new_all_files)
            self._path_to_idx = {path: idx for idx, path in enumerate(new_all_files)}
            self.image_states = new_image_states
            self.labels = new_labels

            cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
            event_system.publish(cmd)

            self.reapply_filters()

            self._last_redraw_time = self._benchmark_timer.elapsed() / 1000.0
            self.benchmarkComplete.emit("Redraw", self._last_redraw_time)

        except (KeyError, IndexError) as e:
            # why: index/path maps can desync if a watchdog removal races with an
            # in-progress remove_images call on the same set of paths.
            logging.error(f"Error removing images: {e}", exc_info=True)

    def ensure_visible(self, original_idx: int, center: bool = False):
        """
        Ensure the thumbnail at the given original index is visible in the scroll area,
        without affecting the selection state.

        Args:
            original_idx: The original index of the thumbnail to make visible
            center: If True, center the thumbnail in the viewport
        """
        # Convert original_idx to visible_idx if filtered
        visible_idx = self._original_to_visible_mapping.get(original_idx)
        if visible_idx is None:
            logging.debug(f"Original index {original_idx} not visible (filtered out)")
            return

        if self._grid_layout_manager:
            self._grid_layout_manager.ensure_widget_visible(visible_idx, center)
        else:
            # Fallback implementation
            # Note: This fallback uses original_idx, but should use visible_idx if filtering is active
            # For now, assuming original_idx maps directly to label key if no grid_layout_manager
            if original_idx not in self.labels:
                logging.debug(f"Original index {original_idx} not found in labels")
                return

            label = self.labels[original_idx]
            if center:
                viewport = self.scroll_area.viewport()
                viewport_height = viewport.height()
                viewport_width = self.scroll_area.viewport().width()
                label_pos = label.mapTo(self._grid_container, QPoint(0, 0))
                x = max(0, label_pos.x() - (viewport_width - label.width()) // 2)
                y = max(0, label_pos.y() - (viewport_height - label.height()) // 2)
                self.scroll_area.horizontalScrollBar().setValue(x)
                self.scroll_area.verticalScrollBar().setValue(y)
            else:
                self.scroll_area.ensureVisible(
                    label.geometry().center().x(),
                    label.geometry().center().y(),
                    label.width() // 2,
                    label.height() // 2
                )

    def closeEvent(self, event):
        """Clean up cache on close."""
        # Stop timers first
        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_filter_update_timer'):
            self._filter_update_timer.stop()
        if hasattr(self, '_priority_update_timer'):
            self._priority_update_timer.stop()
        if hasattr(self, '_preview_tick_timer'):
            self._preview_tick_timer.stop()
        if hasattr(self, '_label_tick_timer'):
            self._label_tick_timer.stop()
        if hasattr(self, '_viewport_executor'):
            # wait=False: in-flight viewport calls are best-effort priority hints; the
            # socket client handles broken-pipe errors on its own after widget teardown.
            self._viewport_executor.shutdown(wait=False)

        # Clear thumbnail cache
        self._thumbnail_cache.clear()

        # Clear widget pool
        for label in self._widget_pool:
            label.deleteLater()
        self._widget_pool.clear()

        super().closeEvent(event)

    def clear_layout(self):
        """Clear layout while maintaining cache."""
        # Stop timers and cleanup thread
        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_filter_update_timer'):
            self._filter_update_timer.stop()
        if hasattr(self, '_priority_update_timer'):
            self._priority_update_timer.stop()
        if hasattr(self, '_preview_tick_timer'):
            self._preview_tick_timer.stop()
        if hasattr(self, '_pending_previews'):
            self._pending_previews.clear()
        if hasattr(self, '_label_tick_timer'):
            self._label_tick_timer.stop()
        if hasattr(self, '_pending_labels'):
            self._pending_labels.clear()
        # Recycle all labels first while they still have proper parent
        for widget in self.labels.values():
            if isinstance(widget, ThumbnailLabel):
                self._recycle_label(widget)

        if self._grid_layout_manager:
            self._grid_layout_manager.clear_layout()

        self.labels.clear()
        cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
        event_system.publish(cmd)
        self.selection_anchor_index = None
        self._selected_indices.clear()
        self._last_preview_selected.clear()

        self.pending_thumbnails.clear()
        self.ready_thumbnails.clear()
        self._initial_thumb_paths.clear()
        self.current_files.clear()
        self.all_files.clear()
        self._all_files_set.clear()
        self._path_to_idx.clear()
        self.current_directory_path = None
        self.image_states.clear()
        self._last_layout_file_count = 0
        # Cancel any in-flight speculative fullres tasks from the old directory
        # so workers don't waste time on files no longer visible.
        if self._last_fullres_pairs and self.socket_client:
            paths_to_cancel = list(self._last_fullres_pairs.keys())
            self._viewport_executor.submit(
                self.socket_client.update_viewport_heatmap,
                [], [], [], paths_to_cancel,
            )
        self._last_thumb_pairs = {}
        self._last_fullres_pairs = {}
        self._hovered_label = None

        if hasattr(self, '_resize_timer'):
            self._resize_timer.start()
        if hasattr(self, '_filter_update_timer'):
            self._filter_update_timer.stop()

    def _thumbnail_generation_callback(self, original_path: str, result: Optional[str], error: Optional[Exception]):
        """
        Callback for RenderManager. Called from a worker thread.
        Loads the generated thumbnail into a QImage off the main thread and
        emits a signal to forward the result to the main GUI thread.
        """
        # QImage is safe to construct off-thread.
        if result and not error:
            image = QImage(result)
            if image.isNull():
                error = RuntimeError(f"Failed to load generated thumbnail: {result}")
                self._thumbnail_generated_signal.emit(original_path, None, error)
            else:
                self._thumbnail_generated_signal.emit(original_path, image, None)
        else:
            # Pass along the original error or create a new one if result is missing.
            if not error:
                error = RuntimeError("Thumbnail generation returned no path and no error.")
            self._thumbnail_generated_signal.emit(original_path, None, error)

    def _on_thumbnail_ready(self, original_path: str, image: Optional[QImage], error: Optional[Exception]):
        """
        Handles thumbnail generation results in the main GUI thread.
        If the UI placeholder isn't ready, it caches the result for later.
        """
        is_error = error or image is None or image.isNull()
        if is_error:
            _err_img = QImage(self.display_size, self.display_size, QImage.Format_RGB32)
            _err_img.fill(QColor(255, 0, 0))
            pixmap = QPixmap.fromImage(_err_img)
        else:
            pixmap = QPixmap.fromImage(image)
        
        if is_error:
            logging.error(f"Thumbnail generation failed for {original_path}", exc_info=bool(error))

        original_idx = self._path_to_idx.get(original_path, -1)
        if original_idx >= 0:
            label = self.labels.get(original_idx)
            if label:
                label.updateThumbnail(pixmap)
                state = self.image_states.get(original_idx)
                if state:
                    state.loaded = not is_error
                    state.prioritized = False
                logging.debug(f"[thumb] applied thumbnail for {os.path.basename(original_path)} (error={is_error})")
            else:
                logging.warning(f"[thumb] no label for original_idx={original_idx} ({os.path.basename(original_path)})")
        else:
            # Race condition: thumbnail arrived before placeholder was created.
            logging.debug(f"Thumbnail for {os.path.basename(original_path)} arrived early, caching.")
            if not is_error:
                self.ready_thumbnails[original_path] = pixmap

        if original_path in self.pending_thumbnails:
            self.pending_thumbnails.remove(original_path)

    _PREVIEW_TICK_BATCH = 20  # QImages loaded per 16ms tick (~60fps)

    def _tick_preview_loading(self):
        """
        Drains up to _PREVIEW_TICK_BATCH items from _pending_previews per timer
        tick.  Loading QImages in small batches lets Qt process paint events
        between ticks, so thumbnails appear progressively instead of all at once.

        Items are sorted by heatmap priority (highest first = closest to cursor)
        before draining, so thumbnails always load in cursor-outward order
        regardless of the order notifications arrived from the daemon.
        """
        if self._last_thumb_pairs and len(self._pending_previews) > self._PREVIEW_TICK_BATCH:
            pmap = self._last_thumb_pairs
            self._pending_previews.sort(key=lambda item: -pmap.get(item[0], 0))

        batch = self._pending_previews[:self._PREVIEW_TICK_BATCH]
        del self._pending_previews[:self._PREVIEW_TICK_BATCH]

        for image_path, thumbnail_path in batch:
            # Skip files not in the current directory (stale notifications from
            # daemon background work) and duplicates already loaded.
            orig_idx = self._path_to_idx.get(image_path, -1)
            if orig_idx < 0:
                continue
            state = self.image_states.get(orig_idx)
            if state and state.loaded:
                continue
            image = QImage(thumbnail_path)
            if not image.isNull():
                self._thumbnail_generated_signal.emit(image_path, image, None)
            else:
                logging.warning(f"Failed to load thumbnail: {thumbnail_path}")

        if not self._pending_previews:
            self._preview_tick_timer.stop()

    def get_benchmark_results(self) -> dict:
        """Return the latest benchmark results"""
        return {
            "Initial Load Time": self._last_load_time,
            "Redraw Time": self._last_redraw_time,
            "Total Images": len(self.current_files),
            "Cached Images": len(self._thumbnail_cache),
            "Pending Images": len(self.pending_thumbnails)
        }

    def handleSelection(self, label: ThumbnailLabel, modifiers: Qt.KeyboardModifiers):
        """Handle selection when user clicks on a thumbnail by publishing a command."""
        if self.hotkey_range_selection_active:
            self.hotkey_range_selection_active = False
            self.setCursor(Qt.ArrowCursor)
            # The selection was already made by mouseMove, so we just exit the mode.
            return

        original_idx = self._label_to_original_idx(label)
        if original_idx is None:
            logging.warning(f"Clicked label {label.original_path} not found in self.labels.")
            return

        paths_to_act_on = set()
        command = None

        if modifiers & Qt.ShiftModifier and self.selection_anchor_index is not None:
            start_pos = self._original_to_visible_mapping.get(self.selection_anchor_index)
            end_pos = self._original_to_visible_mapping.get(original_idx)

            if start_pos is not None and end_pos is not None:
                start, end = min(start_pos, end_pos), max(start_pos, end_pos)
                for i in range(start, end + 1):
                    mapped_idx = self._visible_to_original_mapping.get(i)
                    if mapped_idx is not None:
                        paths_to_act_on.add(self.all_files[mapped_idx])
            command = AddToSelectionCommand(paths=paths_to_act_on, source="thumbnail_view", timestamp=time.time())
        elif modifiers & Qt.ControlModifier:
            paths_to_act_on = {self.all_files[original_idx]}
            command = ToggleSelectionCommand(paths=paths_to_act_on, source="thumbnail_view", timestamp=time.time())
            self.selection_anchor_index = original_idx # Ctrl-click also updates anchor
        else:
            paths_to_act_on = {self.all_files[original_idx]}
            command = ReplaceSelectionCommand(paths=paths_to_act_on, source="thumbnail_view", timestamp=time.time())
            self.selection_anchor_index = original_idx # Plain click sets the anchor

        if command:
            event_system.publish(command)

    def mouseReleaseEvent(self, event):
        """Handle mouse release events."""
        if event.button() == Qt.LeftButton and self._drag_start_index != -1:
            self._commit_selection(self._drag_start_index, self._drag_last_index)
            self._drag_start_index = -1
            self._drag_last_index = -1
            self._selection_mode = None
            self._last_preview_selected = set()

        super().mouseReleaseEvent(event)

    def _update_selection_preview(self, start_idx: int, end_idx: Optional[int]):
        """Visually update thumbnail borders during a drag without changing the core selection state.

        Uses delta tracking: only labels whose highlight state actually changed
        since the last call are touched, avoiding an O(N) scan of all labels.
        """
        if end_idx is None:
            end_idx = start_idx

        preview_indices = self._get_indices_in_range(start_idx, end_idx)
        current_selected_indices = self._selected_indices

        # Compute the full desired-selected set via set math (no label iteration).
        if self._selection_mode == "replace":
            desired = preview_indices
        elif self._selection_mode == "add":
            desired = current_selected_indices | preview_indices
        elif self._selection_mode == "remove":
            desired = current_selected_indices - preview_indices
        else:
            desired = preview_indices

        to_highlight = desired - self._last_preview_selected
        to_unhighlight = self._last_preview_selected - desired

        for idx in to_highlight:
            label = self.labels.get(idx)
            if label and label.isVisible() and not label.selected:
                label.setSelected(True)
        for idx in to_unhighlight:
            label = self.labels.get(idx)
            if label and label.isVisible() and label.selected:
                label.setSelected(False)

        self._last_preview_selected = desired

    def _commit_selection(self, start_idx: int, end_idx: int):
        """Publish the appropriate command to finalize the selection."""
        paths_in_range = self._get_paths_in_range(start_idx, end_idx)
        command = None

        if self._selection_mode == "replace":
            command = ReplaceSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "add":
            command = AddToSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "remove":
            command = RemoveFromSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())

        if command:
            event_system.publish(command)

    def _get_indices_in_range(self, start_idx: int, end_idx: int) -> Set[int]:
        """Helper to get all original indices between a start and end index, respecting the visible order."""
        start_pos = self._original_to_visible_mapping.get(start_idx, -1)
        end_pos = self._original_to_visible_mapping.get(end_idx, -1)
        if start_pos == -1 or end_pos == -1:
            return {start_idx} if start_idx != -1 else set()

        low, high = min(start_pos, end_pos), max(start_pos, end_pos)
        return {self._visible_original_indices[i] for i in range(low, high + 1)}

    def _get_paths_in_range(self, start_idx: int, end_idx: int) -> Set[str]:
        """Convert a range of original indices to file paths."""
        return {self.all_files[idx] for idx in self._get_indices_in_range(start_idx, end_idx)}

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Handles mouse double-click events to open an image in PictureView."""
        if event.button() == Qt.LeftButton:
            hovered_path = self.get_hovered_image_path()
            if hovered_path:
                self.doubleClicked.emit(hovered_path)
                logging.debug(f"Double-clicked on thumbnail, emitting signal for path: {hovered_path}")
            else:
                logging.debug("Double-click, but no image path hovered.")

    def setHighlightedThumbnail(self, image_path: str):
        """Briefly highlight a thumbnail on return from picture view without changing selection."""
        try:
            original_idx = self._path_to_idx.get(image_path, -1)
            if original_idx < 0:
                logging.warning(f"Image {image_path} not found in all_files during highlight attempt.")
                return

            if original_idx in self._original_to_visible_mapping:
                label_to_highlight = self.labels.get(original_idx)
                if label_to_highlight:
                    label_to_highlight.setSelected(True)
                    self.ensure_visible(original_idx, center=True)
                    QTimer.singleShot(1000, lambda: label_to_highlight.setSelected(False))
                else:
                    logging.debug(f"Label for original index {original_idx} not found.")
            else:
                logging.debug(f"Image {image_path} (original index {original_idx}) not currently visible.")

        except (AttributeError, RuntimeError) as e:
            # why: label or scroll bar can be partially torn down if a directory
            # reload races with the highlight timer firing.
            logging.error(f"Error highlighting thumbnail: {e}", exc_info=True)

    def _get_thumbnail_at_pos(self, pos: QPoint) -> Optional[int]:
        """Get the thumbnail index at the given position. Returns original_idx."""
        if not self._grid_layout_manager:
            return None

        # Convert position from this widget's coordinates to the scroll area's viewport
        global_pos = self.mapToGlobal(pos)
        pos_in_viewport = self.scroll_area.viewport().mapFromGlobal(global_pos)

        # Adjust for scroll position to get point relative to the top-left of the content
        h_scroll = self.scroll_area.horizontalScrollBar().value()
        v_scroll = self.scroll_area.verticalScrollBar().value()
        pos_in_viewport_widget = pos_in_viewport + QPoint(h_scroll, v_scroll)

        # Map the point from the viewport widget's coordinates to our centered grid container
        pos_in_grid_container = self._grid_container.mapFrom(self._viewport_widget, pos_in_viewport_widget)

        # Now that we have the correct coordinates, ask the manager for the index
        visible_idx = self._grid_layout_manager.get_widget_at_position(pos_in_grid_container)

        if visible_idx is not None:
            return self._visible_to_original_mapping.get(visible_idx)

        return None

    def apply_filter(self, filter_text: str):
        """
        Sets the text filter and applies all filters.
        """
        self._current_filter = filter_text
        self._filter_update_timer.start()

    def apply_star_filter(self, star_states: list):
        """Sets the star filter and applies all filters."""
        self._current_star_filter = star_states
        self._filter_update_timer.start()

    def reapply_filters(self):
        """
        Re-applies all active filters.  When the scan is still running the
        filter is computed locally (fast path).  Otherwise the daemon is
        queried on a background thread so the GUI never blocks on I/O.
        """
        logging.info(
            f"[chunking] reapply_filters: is_loading={self._is_loading}, "
            f"all_files={len(self.all_files)}, labels={len(self.labels)}, "
            f"pending_labels={len(self._pending_labels)}"
        )

        if not self.all_files or not self.socket_client:
            logging.warning("Cannot apply filters: file list or socket client is not ready.")
            return

        if self._is_loading:
            # Fast path: show everything during the initial scan.
            self._apply_filter_results(set(self.all_files))
            return

        # Async path: submit the socket call to the executor so the GUI
        # stays responsive while the daemon processes the query.
        if self._filter_in_flight:
            self._filter_pending = True
            return

        self._filter_in_flight = True
        # Snapshot the current filter values so racing changes don't corrupt
        # the background call with half-old / half-new state.
        text_filter = self._current_filter
        star_filter = list(self._current_star_filter)
        self._viewport_executor.submit(self._fetch_filtered_paths, text_filter, star_filter)

    def _fetch_filtered_paths(self, text_filter: str, star_filter: list):
        """Runs on _viewport_executor thread — never blocks the GUI."""
        try:
            response = self.socket_client.get_filtered_file_paths(text_filter, star_filter)
            if response and response.status == "success":
                self._filtered_paths_ready.emit(set(response.paths))
            else:
                logging.error(f"Failed to get filtered paths from daemon. Response: {response}")
                self._filtered_paths_ready.emit(None)
        except Exception as e:
            logging.error(f"Error fetching filtered paths: {e}", exc_info=True)
            self._filtered_paths_ready.emit(None)

    @Slot(object)
    def _on_filtered_paths_ready(self, visible_paths):
        """Receives the daemon's filter response on the GUI thread."""
        self._filter_in_flight = False

        if visible_paths is None:
            visible_paths = set(self.all_files)

        self._apply_filter_results(visible_paths)

        # If another filter change arrived while this one was in flight,
        # re-submit with the latest filter values.
        if self._filter_pending:
            self._filter_pending = False
            self.reapply_filters()

    def _apply_filter_results(self, visible_paths: set):
        """Common path for both sync (loading) and async (daemon) filter results."""
        new_hidden_indices = set()
        for i, file_path in enumerate(self.all_files):
            if file_path not in visible_paths:
                new_hidden_indices.add(i)

        hidden_changed = self._hidden_indices != new_hidden_indices
        count_changed = len(self.all_files) != self._last_layout_file_count
        will_update = hidden_changed or count_changed

        logging.info(
            f"[chunking] _apply_filter_results: all_files={len(self.all_files)}, "
            f"labels={len(self.labels)}, visible_paths={len(visible_paths)}, "
            f"hidden={len(new_hidden_indices)}, "
            f"hidden_changed={hidden_changed}, count_changed={count_changed}, "
            f"last_layout_file_count={self._last_layout_file_count}, "
            f"will_update_layout={will_update}"
        )

        # Update layout only if the set of visible items OR the total count has changed
        if will_update:
            self._hidden_indices = new_hidden_indices
            self._update_filtered_layout()
            self._last_layout_file_count = len(self.all_files)
            logging.info(
                f"[chunking] _update_filtered_layout done: "
                f"current_files={len(self.current_files)}, "
                f"labels_in_layout={len(self._visible_to_original_mapping)}"
            )
        else:
            logging.info(
                f"[chunking] _apply_filter_results: SKIPPED layout update "
                f"(labels={len(self.labels)} but layout has {self._last_layout_file_count} files)"
            )

            total_count = len(self.all_files)
            visible_count = len(self.current_files)
            hidden_count = total_count - visible_count
            logging.info(f"Filter: '{self._current_filter}' applied. Visible images: {visible_count}/{total_count}")
            if hidden_count > 0:
                logging.info(f"  {hidden_count} images hidden")

            status_msg = f"Filter: '{self._current_filter}' - {visible_count}/{total_count} images displayed"
            if hidden_count > 0:
                status_msg += f" ({hidden_count} hidden)"
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="thumbnail_view",
                timestamp=time.time(),
                message=status_msg,
                timeout=4000
            ))
            self.filtersApplied.emit()

    def _update_filtered_layout(self):
        """
        Efficiently updates the layout by showing/hiding widgets instead of rebuilding.
        """
        if not self._grid_layout_manager:
            return

        self.current_files = []
        self._visible_to_original_mapping.clear()
        self._original_to_visible_mapping.clear()
        self._visible_original_indices.clear()

        visible_idx = 0
        for original_idx, file_path in enumerate(self.all_files):
            label = self.labels.get(original_idx)
            if label:
                if original_idx in self._hidden_indices:
                    label.hide()
                else:
                    label.show()
                    self.current_files.append(file_path)
                    self._visible_to_original_mapping[visible_idx] = original_idx
                    self._original_to_visible_mapping[original_idx] = visible_idx
                    self._visible_original_indices.append(original_idx)
                    visible_idx += 1

        # Clear hover if the hovered label is now hidden
        if self._hovered_label is not None:
            hovered_orig_idx = self._label_to_original_idx(self._hovered_label)
            if hovered_orig_idx is not None and hovered_orig_idx in self._hidden_indices:
                self._hovered_label = None
                self.thumbnailLeft.emit()

        visible_labels = {i: self.labels[self._visible_to_original_mapping[i]] for i in range(len(self.current_files))}
        self._grid_layout_manager.set_files_and_labels(self.current_files, visible_labels)
        self._grid_layout_manager.update_layout()

        # On the first layout after load, seed the heatmap immediately so
        # _last_thumb_pairs is populated before notifications start draining.
        # Subsequent updates fire once after a short delay.
        if self._needs_heatmap_seed:
            self._needs_heatmap_seed = False
            self._prioritize_visible_thumbnails()
        else:
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)

    def _on_scroll(self, value):
        """Slot to handle scroll bar value changes.

        Starts a repeating heatmap timer so thumbnails update continuously
        during scrolling.  A separate idle timer stops the repeating timer
        200ms after the last scroll event.
        """
        if not self._priority_update_timer.isActive():
            self._prioritize_visible_thumbnails()  # immediate first update
            self._priority_update_timer.start()
        self._scroll_idle_timer.start()  # reset idle countdown

    def _on_scroll_idle(self):
        """Called when no scroll events have fired for 200ms — stop the
        repeating heatmap timer and fire one final update."""
        self._priority_update_timer.stop()
        self._prioritize_visible_thumbnails()

    def _prioritize_visible_thumbnails(self):
        """Computes heatmap priorities around the cursor and sends per-path
        priority pairs to the daemon for both thumbnails and speculative fullres.

        Only paths whose priority actually changed (or that entered/left the
        zone) are sent, keeping the daemon call small even on single-cell moves.
        Stale IPC calls are dropped via a generation counter so the daemon never
        processes an outdated viewport position.

        During a new-folder scan, all thumbnail requests are suppressed so
        workers stay free for directory discovery.  For cached folders the
        full visible viewport is requested at GUI_REQUEST_LOW.
        """
        if not self.socket_client or not self.labels or not self.current_files or not self._grid_layout_manager:
            return

        columns = self._grid_layout_manager.columns
        if columns <= 0:
            return

        # --- Determine heatmap center ---
        ref_visible_idx = None
        if self._hovered_label:
            hovered_orig_idx = self._label_to_original_idx(self._hovered_label)
            if hovered_orig_idx is not None:
                ref_visible_idx = self._original_to_visible_mapping.get(hovered_orig_idx)

        if ref_visible_idx is None:
            first_row, last_row = self._grid_layout_manager.get_visible_rows()
            first_row = max(0, first_row - 1)
            last_row += 1
            start_idx = first_row * columns
            end_idx = min(len(self.current_files) - 1, (last_row + 1) * columns - 1)
            ref_visible_idx = (start_idx + end_idx) // 2

        center_row, center_col = divmod(ref_visible_idx, columns)

        # --- Build loaded_set scoped to the heatmap bounding box ---
        total_visible = len(self.current_files)
        total_rows = (total_visible + columns - 1) // columns if total_visible > 0 else 0
        bb_min_row = max(0, center_row - THUMB_RING_COUNT)
        bb_max_row = min(total_rows - 1, center_row + THUMB_RING_COUNT) if total_rows > 0 else 0
        bb_min_col = max(0, center_col - THUMB_RING_COUNT)
        bb_max_col = min(columns - 1, center_col + THUMB_RING_COUNT)

        loaded_set: Set[int] = set()
        for r in range(bb_min_row, bb_max_row + 1):
            for c in range(bb_min_col, bb_max_col + 1):
                vis_idx = r * columns + c
                if vis_idx >= total_visible:
                    continue
                orig_idx = self._visible_to_original_mapping.get(vis_idx)
                if orig_idx is not None:
                    state = self.image_states.get(orig_idx)
                    if state and state.loaded:
                        loaded_set.add(vis_idx)

        # --- Compute heatmap ---
        thumb_pairs, fullres_pairs = compute_heatmap(
            center_row, center_col, columns,
            total_visible, loaded_set,
        )

        # --- Map visible_idx → file path, build {path: priority} dicts ---
        vis_to_orig = self._visible_to_original_mapping.get
        all_files = self.all_files

        current_thumb: dict[str, int] = {}
        for vis_idx, priority in thumb_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                current_thumb[all_files[orig_idx]] = priority

        # --- Cached/post-scan: request full viewport at GUI_REQUEST_LOW ---
        # When no scan is competing for workers, aggressively request the
        # entire visible viewport so cached thumbnails appear immediately.
        if not self._is_loading:
            first_row, last_row = self._grid_layout_manager.get_visible_rows()
            first_row = max(0, first_row - 1)
            last_row += 1
            vis_start = first_row * columns
            vis_end = min(total_visible - 1, (last_row + 1) * columns - 1)
            for vis_idx in range(vis_start, vis_end + 1):
                orig_idx = vis_to_orig(vis_idx)
                if orig_idx is None:
                    continue
                state = self.image_states.get(orig_idx)
                if state and state.loaded:
                    continue
                path = all_files[orig_idx]
                if path not in current_thumb:
                    current_thumb[path] = 40  # GUI_REQUEST_LOW

        current_fullres: dict[str, int] = {}
        for vis_idx, priority in fullres_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                current_fullres[all_files[orig_idx]] = priority

        # --- Early out: skip IPC when every (path, priority) pair is identical ---
        if current_thumb == self._last_thumb_pairs and current_fullres == self._last_fullres_pairs:
            return

        # --- Compute deltas: only send paths whose priority changed or that entered/left ---
        prev_thumb = self._last_thumb_pairs
        prev_fullres = self._last_fullres_pairs

        # Thumb upgrades: new paths OR paths whose priority changed.
        delta_upgrade: list[tuple[str, int]] = [
            (p, pri) for p, pri in current_thumb.items()
            if prev_thumb.get(p) != pri
        ]
        # Thumb downgrades: paths that left the zone entirely.
        paths_to_downgrade = [p for p in prev_thumb if p not in current_thumb]

        # Fullres requests: new or priority-changed.
        delta_fullres: list[tuple[str, int]] = [
            (p, pri) for p, pri in current_fullres.items()
            if prev_fullres.get(p) != pri
        ]
        # Fullres cancels: paths that left the zone.
        fullres_to_cancel = [p for p in prev_fullres if p not in current_fullres]

        # --- Send to daemon (with stale-request protection) ---
        if delta_upgrade or paths_to_downgrade or delta_fullres or fullres_to_cancel:
            self._viewport_generation += 1
            gen = self._viewport_generation
            def _send_if_current(generation, up, down, fr, fc):
                if self._viewport_generation != generation:
                    return
                self.socket_client.update_viewport_heatmap(up, down, fr, fc)

            self._viewport_executor.submit(
                _send_if_current, gen,
                delta_upgrade, paths_to_downgrade,
                delta_fullres, fullres_to_cancel,
            )

        self._last_thumb_pairs = current_thumb
        self._last_fullres_pairs = current_fullres

    def get_visible_count(self) -> int:
        """Returns the number of currently visible thumbnails."""
        return len(self.current_files)

    def filter_affects_rating(self) -> bool:
        """Return True if the active filter could change visibility based on image rating."""
        return not all(self._current_star_filter)

