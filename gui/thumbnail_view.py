from __future__ import annotations
import os
import time
import logging
logger = logging.getLogger(__name__)
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Set
from PySide6.QtCore import (
    Qt, Signal, QTimer, QElapsedTimer, QPoint, QPointF, QEvent, Slot
)
from PySide6.QtGui import QPixmap, QImage, QColor, QMouseEvent, QCursor, QPainter, QTransform
from gui.color_profile import apply_profile_pixmap
from PySide6.QtWidgets import (
    QLabel, QVBoxLayout, QScrollArea, QWidget, QFrame
)

_ValidationErrors = (ValueError, TypeError, KeyError)
from gui.picture_base import PictureBase
from gui.components.item_card import ItemCard
from gui.components.virtual_grid_manager import VirtualGridManager
from core.selection import ReplaceSelectionCommand, AddToSelectionCommand, RemoveFromSelectionCommand
from core.event_system import (event_system, EventType, EventData, InspectorEventData,
    SelectionChangedEventData, StatusMessageEventData, ThumbnailHoveredEventData,
    TextFilterEventData, StarFilterEventData, TagFilterEventData)
from network.daemon_signals import DaemonSignals
from core.notifications import PreviewsReadyData, ScanProgressData, ScanCompleteData, FilesRemovedData
from core.heatmap import compute_heatmap, THUMB_RING_COUNT
from core.priority import Priority
from core.event_system import ThumbnailOverlayEventData
from gui.overlay_manager import OverlayManager, OverlayDescriptor, BULK_THRESHOLD
from gui.overlay_renderers import render_stars, render_badge
from core.file_grouping import FileGroup, build_group_map, expand_paths_for_group
from core.folder_node import FolderNode

from dataclasses import dataclass

@dataclass
class ImageState:
    loaded: bool = False
    prioritized: bool = False

class ThumbnailLabel(ItemCard):

    def __init__(self, file_path: str, size: int, config: dict):
        super().__init__(size, config)
        self.file_path = file_path
        self.original_path = file_path
        self.size = size
        self.loaded = False
        self.config = config

        self._original_idx: int = -1
        self._overlay_manager: OverlayManager | None = None

        # Folder card state
        self.is_folder: bool = False
        self._folder_node: FolderNode | None = None
        self._folder_preview_pixmaps: list | None = None  # cached scaled QPixmaps

        # Throttle inspector events to ~60 fps so rapid mouse movement does not
        # flood the event system and block the GUI thread with socket calls.
        self._pending_norm_pos: Optional[QPointF] = None
        self._inspector_timer = QTimer(self)
        self._inspector_timer.setSingleShot(True)
        self._inspector_timer.setInterval(16)  # ~60 fps
        self._inspector_timer.timeout.connect(self._flushInspectorEvent)

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
            event.ignore()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, 'is_folder', False):
            self._queueFolderInspectorEvent(event.position())
        else:
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
            logger.error("Error queuing inspector event from thumbnail: %s", e, exc_info=True)

    def _flushInspectorEvent(self):
        # Folder cards use a separate image path from the folder's contents
        folder_image = getattr(self, '_pending_folder_image', None)
        if folder_image:
            self._pending_folder_image = None
            self._pending_norm_pos = None
            event_system.publish(InspectorEventData(
                event_type=EventType.INSPECTOR_UPDATE,
                source="thumbnail_view",
                timestamp=time.time(),
                image_path=folder_image,
                normalized_position=QPointF(0.5, 0.5),
            ))
            return
        pos = self._pending_norm_pos
        if pos is None:
            return
        self._pending_norm_pos = None
        event_system.publish(InspectorEventData(
            event_type=EventType.INSPECTOR_UPDATE,
            source="thumbnail_view",
            timestamp=time.time(),
            image_path=self.original_path,
            normalized_position=pos,
        ))

    def _queueFolderInspectorEvent(self, pos: QPointF):
        """Map X position to a folder image and publish an inspector event."""
        node = getattr(self, '_folder_node', None)
        if not node or not node.image_paths:
            return
        try:
            widget_rect = self.rect()
            if widget_rect.width() <= 0:
                return
            norm_x = max(0.0, min(1.0, pos.x() / widget_rect.width()))
            idx = min(int(norm_x * len(node.image_paths)), len(node.image_paths) - 1)
            image_path = node.image_paths[idx]
            # Publish with centered position so the inspector shows the full image
            self._pending_norm_pos = QPointF(0.5, 0.5)
            self._pending_folder_image = image_path
            if not self._inspector_timer.isActive():
                self._inspector_timer.start()
        except (AttributeError, TypeError) as e:
            logger.error("Error queuing folder inspector event: %s", e, exc_info=True)

    def _flushFolderInspectorEvent(self):
        """Flush a pending folder inspector event."""
        image_path = getattr(self, '_pending_folder_image', None)
        if not image_path:
            return
        self._pending_folder_image = None
        event_data = InspectorEventData(
            event_type=EventType.INSPECTOR_UPDATE,
            source="thumbnail_view",
            timestamp=time.time(),
            image_path=image_path,
            normalized_position=QPointF(0.5, 0.5),
        )
        event_system.publish(event_data)

    def paintEvent(self, event):
        if getattr(self, 'is_folder', False):
            self._paint_folder_card(event)
            return
        super().paintEvent(event)
        if self._overlay_manager and self._overlay_manager.has_overlays(self._original_idx):
            try:
                painter = QPainter(self)
                self._overlay_manager.paint(painter, self.rect(), self._original_idx)
                painter.end()
            except Exception:
                # why: renderer crash must not leave QPainter open or break label rendering
                logger.exception("[overlay] paintEvent error for idx %d", self._original_idx)

    # Class-level constants for folder card rendering (avoid per-paint allocation)
    _FOLDER_ICON_FONT = None
    _FOLDER_BADGE_FONT = None
    _FOLDER_BADGE_FM = None
    _FOLDER_ICON_PEN = None
    _FOLDER_BADGE_BG = None
    _FOLDER_BADGE_FG = None

    @classmethod
    def _ensure_folder_fonts(cls):
        if cls._FOLDER_ICON_FONT is None:
            from PySide6.QtGui import QFont, QPen, QBrush, QFontMetrics
            cls._FOLDER_ICON_FONT = QFont("sans-serif", 14)
            cls._FOLDER_BADGE_FONT = QFont("sans-serif", 9, QFont.Bold)
            cls._FOLDER_BADGE_FM = QFontMetrics(cls._FOLDER_BADGE_FONT)
            cls._FOLDER_ICON_PEN = QPen(QColor(200, 200, 200, 200))
            cls._FOLDER_BADGE_BG = QBrush(QColor(0, 0, 0, 160))
            cls._FOLDER_BADGE_FG = QPen(QColor(255, 255, 255))

    def _paint_folder_card(self, event):
        """Render a folder card: 2x2 preview mosaic, folder icon, count badge."""
        from PySide6.QtCore import QRectF

        self._ensure_folder_fonts()
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        node = self._folder_node

        # Draw 2x2 preview mosaic from cached pixmaps
        if self._folder_preview_pixmaps:
            inset = 6
            gap = 2
            available = rect.width() - 2 * inset
            cell = (available - gap) // 2
            for i, scaled in enumerate(self._folder_preview_pixmaps):
                row, col = divmod(i, 2)
                cx = inset + col * (cell + gap)
                cy = inset + row * (cell + gap)
                dx = cx + (cell - scaled.width()) // 2
                dy = cy + (cell - scaled.height()) // 2
                painter.drawPixmap(dx, dy, scaled)

        # Folder icon (bottom-left corner)
        painter.setFont(self._FOLDER_ICON_FONT)
        painter.setPen(self._FOLDER_ICON_PEN)
        painter.drawText(6, rect.height() - 6, "\U0001F4C1")

        # Count badge (top-right corner)
        if node:
            count = node.recursive_count or node.image_count
            if count > 0:
                badge_text = str(count)
                painter.setFont(self._FOLDER_BADGE_FONT)
                fm = self._FOLDER_BADGE_FM
                tw = fm.horizontalAdvance(badge_text) + 8
                th = fm.height() + 4
                badge_rect = QRectF(rect.width() - tw - 4, 4, tw, th)
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._FOLDER_BADGE_BG)
                painter.drawRoundedRect(badge_rect, 4, 4)
                painter.setPen(self._FOLDER_BADGE_FG)
                painter.drawText(badge_rect, Qt.AlignCenter, badge_text)

        painter.end()

    def _cache_folder_previews(self):
        """Load and cache scaled preview pixmaps for this folder card."""
        node = self._folder_node
        if not node or not node.preview_paths:
            self._folder_preview_pixmaps = None
            return
        rect = self.rect() if self.rect().width() > 0 else None
        if not rect:
            self._folder_preview_pixmaps = None
            return
        inset = 6
        gap = 2
        cell = (rect.width() - 2 * inset - gap) // 2
        pixmaps = []
        for thumb_path in node.preview_paths[:4]:
            pix = QPixmap(thumb_path)
            if not pix.isNull():
                pixmaps.append(pix.scaled(cell, cell, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._folder_preview_pixmaps = pixmaps if pixmaps else None

    def _style_border_radius(self) -> int:
        return 4 if getattr(self, 'is_folder', False) else 0

class ThumbnailViewWidget(QFrame):
    doubleClicked = Signal(str)
    thumbnailHovered = Signal(str)  # emits original_path on Enter
    thumbnailLeft = Signal()         # emits when hover ends (no path)
    benchmarkComplete = Signal(str, float)
    filtersApplied = Signal()
    _thumbnail_generated_signal = Signal(str, QImage, object)
    # Dedicated signal for the DB-response file list so it always triggers an
    # immediate layout update, regardless of what fast-scan batches arrived first.
    _initial_files_signal = Signal(list)
    _initial_thumbs_signal = Signal(dict)
    _initial_folders_signal = Signal(list)   # list of FolderNode
    _filtered_paths_ready = Signal(object)  # set of visible paths from daemon
    folderNavigated = Signal(str)            # emitted when user navigates into a folder

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.config_manager = config_manager
        self.gui_config = config_manager.get(
            "gui", {}) if config_manager else {}
        self.display_size = int(config_manager.get("thumbnail_size", 128))
        self.cache_dir = os.path.expanduser(config_manager.get("cache_dir"))
        self.spacing = self.gui_config.get("spacing", 5)
        self.service = None
        self.current_directory_path: Optional[str] = None

        # VirtualGridManager is created in _setupUI
        self._virtual_grid: Optional[VirtualGridManager] = None

        self.labels: Dict[int, ThumbnailLabel] = {}  # original_idx → materialized label (viewport only)
        self.pending_thumbnails = set()
        self._pixmap_cache: Dict[int, QPixmap] = {}  # original_idx → thumbnail pixmap
        self._thumb_path_cache: Dict[int, str] = {}  # original_idx → thumbnail file path (lazy-loaded)
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
        self._range_selection_active = False
        self._drag_shift_pending = False

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
        self._pool_size = 150  # ~2x max materialized labels (buffer + visible rows)

        self._last_resize_size = self.size()

        # Filter state
        self._current_filter = ""
        self._current_star_filter = [True, True, True, True, True, True]
        self._current_tag_filter: List[str] = []
        self._clip_search_paths = None  # set of paths when CLIP search active
        self._person_filter_paths = None  # set of paths when person filter active
        self._hidden_indices = set()
        self._visible_to_original_mapping = {}
        self._original_to_visible_mapping = {}
        self._visible_original_indices: List[int] = []
        self._last_layout_file_count = 0
        self._last_thumb_pairs: dict[str, int] = {}   # path → priority from last heatmap
        self._last_fullres_pairs: dict[str, int] = {}  # path → priority from last heatmap
        self._stale_request_ts: dict[str, float] = {}  # path → monotonic time first seen unloaded after request
        self._STALE_THRESHOLD_S = 5.0  # seconds before re-requesting an unloaded thumbnail
        self._viewport_generation: int = 0  # monotonic counter; stale IPC calls check this

        # RAW+JPG group mode state
        self._group_mode: bool = False
        self._group_map: Dict[str, FileGroup] = {}       # primary_path → FileGroup
        self._grouped_raw_paths: Set[str] = set()       # RAW paths hidden by grouping
        self._raw_to_primary: Dict[str, str] = {}       # raw_path → primary_path

        self.image_states: Dict[int, ImageState] = {}

        # Folder navigation state
        self._folder_nodes: Dict[str, FolderNode] = {}
        self._navigation_stack: List[str] = []

        # Startup timing — reset in load_directory, logged at each pipeline milestone.
        self._startup_t0: Optional[float] = None
        self._startup_first_scan_progress: bool = False
        self._needs_heatmap_seed: bool = False
        self._startup_first_previews_ready: bool = False
        self._startup_thumbnails_emitted: bool = False
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

        # Coalesce rapid scan batches into fewer layout updates.
        # singleShot: fires 250ms after first batch in a burst, NOT restarted
        # per batch, so flushes happen every ~250ms during continuous scanning.
        self._scan_coalesce_timer = QTimer(self)
        self._scan_coalesce_timer.setSingleShot(True)
        self._scan_coalesce_timer.setInterval(250)
        self._scan_coalesce_timer.timeout.connect(self._flush_scan_layout)
        self._scan_batch_pending = False
        self._scan_first_batch_flushed = False
        self._scan_active = False  # True from load_directory to scan_complete

        self._viewport_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viewport")

        event_system.subscribe(EventType.SELECTION_CHANGED, self._on_selection_changed)
        self._on_shift_pressed = lambda _: self._handle_shift_pressed()
        self._on_shift_released = lambda _: self._handle_shift_released()
        event_system.subscribe(EventType.SHIFT_PRESSED, self._on_shift_pressed)
        event_system.subscribe(EventType.SHIFT_RELEASED, self._on_shift_released)
        event_system.subscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
        event_system.subscribe(EventType.FACE_PERSON_FILTER, self._on_face_person_filter)
        event_system.subscribe(EventType.TEXT_FILTER_CHANGED, self._on_text_filter_event)
        event_system.subscribe(EventType.STAR_FILTER_CHANGED, self._on_star_filter_event)
        event_system.subscribe(EventType.TAG_FILTER_CHANGED, self._on_tag_filter_event)
        event_system.subscribe(EventType.CLEAR_FILTERS, self._on_clear_filters_event)

        self.overlay_manager = OverlayManager(request_update=self._request_label_update)
        self.overlay_manager.register_renderer("stars", render_stars)
        self.overlay_manager.register_renderer("badge", render_badge)
        self._daemon_signals: Optional[DaemonSignals] = None
        self._initial_files_signal.connect(self._on_initial_files_received)
        self._initial_thumbs_signal.connect(self._on_initial_thumbs_received)
        self._initial_folders_signal.connect(self._on_initial_folders_received)
        self._filtered_paths_ready.connect(self._on_filtered_paths_ready)

        self._hovered_label: Optional[ThumbnailLabel] = None
        self._thumbnail_generated_signal.connect(self._on_thumbnail_ready, Qt.QueuedConnection)

        self._is_loading = False
        # why: separate from _is_loading — cached folders clear _is_loading in
        # _on_initial_files_received (immediate), uncached folders wait for scan_complete.
        self._folder_is_cached = False
        self._filter_in_flight = False
        self._filter_pending = False


    def _initializeLayout(self):
        if self._virtual_grid:
            self._virtual_grid.update_layout()

    def set_service(self, service):
        self.service = service

    def _set_hovered_label(self, label: ThumbnailLabel):
        if self._hovered_label != label:
            self._hovered_label = label
            self.thumbnailHovered.emit(label.original_path)
            event_system.publish(ThumbnailHoveredEventData(
                event_type=EventType.THUMBNAIL_HOVERED, source="thumbnail_view",
                timestamp=time.time(), path=label.original_path))
            if getattr(label, 'is_folder', False) and label._folder_node:
                node = label._folder_node
                count = node.recursive_count or node.image_count
                event_system.publish(StatusMessageEventData(
                    event_type=EventType.STATUS_MESSAGE, source="thumbnail_view",
                    timestamp=time.time(),
                    message=f"{node.name} ({count} images)",
                    timeout=0))
            self._priority_update_timer.start()

    def _clear_hovered_label(self, label: ThumbnailLabel):
        if self._hovered_label == label:
            self._hovered_label = None
            self.thumbnailLeft.emit()
            event_system.publish(EventData(
                event_type=EventType.THUMBNAIL_LEFT, source="thumbnail_view",
                timestamp=time.time()))
            self._priority_update_timer.start()

    @property
    def folder_paths(self) -> Set[str]:
        """Paths currently displayed as folder cards in the grid."""
        return self._folder_nodes.keys()

    def get_hovered_image_path(self) -> Optional[str]:
        if self._hovered_label:
            return self._hovered_label.original_path
        return None

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)

        if self.middle_mouse_pressed:
            delta = event.pos() - self.middle_mouse_press_pos
            h_bar = self.scroll_area.horizontalScrollBar()
            v_bar = self.scroll_area.verticalScrollBar()

            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())

            self.middle_mouse_press_pos = event.pos()

        if self._range_selection_active and self.selection_anchor_index is not None:
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
            label.hide()
            label.setPixmap(QPixmap())
            label.loaded = False
            label.setSelected(False)
            self.overlay_manager.remove_all_for_idx(label._original_idx)
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
            label.is_folder = False
            label._folder_node = None
            label.show()
        else:
            label = ThumbnailLabel(file_path, self.display_size, self.gui_config)

        label._original_idx = original_idx
        label._overlay_manager = self.overlay_manager
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
                        self._restore_pre_click_selection()
                        self._emit_double_click(hovered_path)
                    return True  # Event handled
            return False
        elif obj == self:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                mouse_event = QMouseEvent(event)
                if mouse_event.button() == Qt.LeftButton:
                    hovered_path = self.get_hovered_image_path()
                    if hovered_path:
                        self._restore_pre_click_selection()
                        self._emit_double_click(hovered_path)
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
            self._pre_click_selection = set(self._current_selection)
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
                # Seed the preview tracker with the current selection so that
                # delta tracking in _update_selection_preview will unhighlight
                # previously-selected labels on the first preview call.
                self._last_preview_selected = set(self._selected_indices)

            self._update_selection_preview(start_index, start_index)

        super().mousePressEvent(event)

    def _end_range_selection(self):
        if self._range_selection_active:
            self._range_selection_active = False
            self.setCursor(Qt.ArrowCursor)
            current_pos = self.mapFromGlobal(QCursor.pos())
            end_idx = self._get_thumbnail_at_pos(current_pos)
            self._commit_selection(self.selection_anchor_index, end_idx)
            self.selection_anchor_index = None
            self._selection_mode = None

    def _handle_shift_pressed(self):
        # why: defer mode switch to mouseReleaseEvent so drag preview isn't disrupted
        if self._drag_start_index != -1 and not self._range_selection_active:
            self._drag_shift_pending = True

    def _handle_shift_released(self):
        if self._range_selection_active:
            self._end_range_selection()
        self._drag_shift_pending = False

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

    def _recompute_selected_indices(self):
        """Rebuild _selected_indices from the path-based _current_selection after an index remap."""
        self._selected_indices = {
            self._path_to_idx[p] for p in self._current_selection
            if p in self._path_to_idx
        }

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

        self._grid_container = QWidget()
        self._grid_container.setContentsMargins(0, 0, 0, 0)

        self._virtual_grid = VirtualGridManager(
            self._grid_container,
            self.scroll_area,
            self.display_size,
            self.spacing,
        )
        self.scroll_area.setWidget(self._grid_container)
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
        if self._virtual_grid:
            self._virtual_grid.update_layout()
            self._sync_virtual_viewport()
        self._priority_update_timer.start()

    def load_directory(self, directory_path: str, recursive: bool = True):
        self._startup_t0 = time.perf_counter()
        self._startup_first_scan_progress = False
        self._startup_first_previews_ready = False
        self._startup_thumbnails_emitted = False
        self._startup_inline_thumb_count = 0
        logger.info("[startup] load_directory called for %s", directory_path)
        self.clear_layout()
        # Set scan state AFTER clear_layout() which resets _scan_active to False
        self._scan_batch_pending = False
        self._scan_first_batch_flushed = False
        self._scan_active = True
        self._scan_coalesce_timer.stop()
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
        logger.info("Querying daemon for files in: %s (Recursive: %s)", directory_path, recursive)
        thread = threading.Thread(target=self._get_files_from_daemon, args=(directory_path, recursive), daemon=True)
        thread.start()

    def _get_files_from_daemon(self, directory_path: str, recursive: bool = True):
        response = self.service.get_directory_files(directory_path, recursive)
        if response:
            files = response.get('files', [])
            thumbnail_paths = response.get('thumbnail_paths', {})
            thumb_count = len(thumbnail_paths)
            logger.info(
                "[trace] daemon response: files=%d, thumbs=%d for %s",
                len(files), thumb_count, directory_path,
            )

            # Discover subdirectories for non-recursive browsing
            if not recursive:
                folder_nodes = self.service.get_subdirectories(directory_path)
                if folder_nodes:
                    logger.info("[folders] found %d subdirectories in %s", len(folder_nodes), directory_path)
                    self._initial_folders_signal.emit(folder_nodes)

            # Emit via the dedicated signal so the DB-response batch always shows
            # placeholders immediately, even if fast-scan notifications arrived first
            # and consumed the is_first_batch shortcut in _add_image_batch.
            self._initial_files_signal.emit(sorted(files))
            # Feed cached thumbnail paths directly into the preview pipeline
            # so the GUI loads QImages from local cache without a daemon round-trip.
            if thumbnail_paths:
                logger.info("[startup] %d cached thumbnail paths from initial response", len(thumbnail_paths))
                self._initial_thumbs_signal.emit(thumbnail_paths)
        else:
            logger.error("Failed to request file list for %s from daemon. Response: %s", directory_path, response)

    @Slot(list)
    def _on_initial_files_received(self, files: list):
        """Handles the DB-response file list.  Populates data structures and
        triggers a layout update; actual widget creation is deferred to virtual
        viewport sync.
        """
        self._folder_is_cached = len(files) > 0
        if self._folder_is_cached:
            self._is_loading = False
        logger.info(
            "[trace] _on_initial_files_received: %d files, cached=%s, is_loading=%s",
            len(files), self._folder_is_cached, self._is_loading,
        )
        if not files:
            return
        self._add_image_batch(files)
        if self.all_files:
            self.reapply_filters()

    @Slot(list)
    def _on_initial_folders_received(self, folder_nodes: list):
        """Insert folder entries at the start of the grid (before images)."""
        if not folder_nodes:
            return
        folder_paths = []
        for node in folder_nodes:
            self._folder_nodes[node.path] = node
            folder_paths.append(node.path)
        # Prepend folders so they appear at the top of the grid.
        # _add_image_batch deduplicates via _all_files_set.
        self._add_image_batch(folder_paths)
        logger.info("[folders] inserted %d folder cards into grid", len(folder_paths))

    @Slot(dict)
    def _on_initial_thumbs_received(self, thumb_map: dict):
        for source_path, thumb_path in thumb_map.items():
            orig_idx = self._path_to_idx.get(source_path, -1)
            if orig_idx < 0:
                # Files not yet in all_files — store for later
                self._initial_thumb_paths[source_path] = thumb_path
                continue
            self._thumb_path_cache[orig_idx] = thumb_path

            # If this label is already materialized (placeholder), load the
            # pixmap now.  The files signal is queued before thumbs, so labels
            # are typically created as placeholders before thumb paths arrive.
            label = self.labels.get(orig_idx)
            if label and orig_idx not in self._pixmap_cache:
                image = QImage(thumb_path)
                if not image.isNull():
                    image = self._apply_db_orientation(image, source_path)
                    pixmap = apply_profile_pixmap(image)
                    self._pixmap_cache[orig_idx] = pixmap
                    label.updateThumbnail(pixmap)
                    label.loaded = True
                    state = self.image_states.get(orig_idx)
                    if state:
                        state.loaded = True

    def _request_label_update(self, idx: int) -> None:
        label = self.labels.get(idx)
        if label:
            label.update()

    def _on_overlay_event(self, event_data: ThumbnailOverlayEventData) -> None:
        action = event_data.action
        paths = event_data.paths
        logger.debug("[overlay] _on_overlay_event: action=%s, paths=%d, renderer=%s",
                      action, len(paths), event_data.renderer_name)

        if action == "show":
            # Degradation gate: for large transient batches, fall back to a status message.
            if event_data.duration is not None and len(paths) > BULK_THRESHOLD:
                event_system.publish(StatusMessageEventData(
                    event_type=EventType.STATUS_MESSAGE,
                    source="overlay_manager",
                    timestamp=time.time(),
                    message=f"{event_data.renderer_name} overlay applied to {len(paths)} images",
                    timeout=2000,
                ))
                return

            descriptor = OverlayDescriptor(
                overlay_id=event_data.overlay_id,
                renderer_name=event_data.renderer_name,
                params=event_data.params,
                position=event_data.position,
                duration=event_data.duration,
            )
            matched = 0
            for path in paths:
                idx = self._path_to_idx.get(path)
                if idx is not None:
                    self.overlay_manager.show(idx, descriptor)
                    label = self.labels.get(idx)
                    if label:
                        label.update()
                        matched += 1
            logger.debug("[overlay] show: %d/%d paths matched to labels", matched, len(paths))

        elif action == "remove":
            for path in paths:
                idx = self._path_to_idx.get(path)
                if idx is not None:
                    self.overlay_manager.remove(idx, event_data.overlay_id)
                    label = self.labels.get(idx)
                    if label:
                        label.update()

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._daemon_signals = daemon_signals
        daemon_signals.previews_ready.connect(self._on_previews_ready)
        daemon_signals.scan_progress.connect(self._on_scan_progress)
        daemon_signals.files_removed.connect(self._on_files_removed)
        daemon_signals.scan_complete.connect(self._on_scan_complete)

    @Slot(object)
    def _on_previews_ready(self, data: PreviewsReadyData) -> None:
        logger.debug("[trace] previews_ready: all_files=%d, is_loading=%s", len(self.all_files), self._is_loading)
        if not self._startup_first_previews_ready and self._startup_t0 is not None:
            self._startup_first_previews_ready = True
            elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
            logger.info("[startup] first previews_ready: %.0f ms after load_directory", elapsed_ms)
        image_path = data.image_entry.path
        logger.info("ThumbnailViewWidget received notification: Previews ready for %s", image_path)
        if data.thumbnail_path:
            # Skip notifications for files not in the current directory.
            # Daemon background work (watchdog, previous sessions) can produce
            # previews_ready for unrelated files that waste tick slots.
            if image_path not in self._path_to_idx:
                return
            # Buffer the path instead of loading QImage immediately.  Draining
            # the buffer via _preview_tick_timer lets the event loop repaint
            # between batches, producing smooth progressive thumbnail reveal
            # rather than a single large batch.
            self._pending_previews.append((image_path, data.thumbnail_path))
            if not self._preview_tick_timer.isActive():
                self._preview_tick_timer.start()
        else:
            logger.debug("[thumb] previews_ready has no thumbnail_path for %s", os.path.basename(image_path))

    @Slot(object)
    def _on_scan_progress(self, data: ScanProgressData) -> None:
        logger.debug("[trace] scan_progress: all_files=%d, is_loading=%s", len(self.all_files), self._is_loading)
        try:
            first_batch = not self._startup_first_scan_progress
            if first_batch and self._startup_t0 is not None:
                self._startup_first_scan_progress = True
                elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                logger.info("[startup] first scan_progress: %.0f ms after load_directory (%d files in batch)", elapsed_ms, len(data.files))
            logger.info("Received scan_progress batch for '%s' with %d files.", data.path, len(data.files))
            self._add_image_batch(sorted(f.path for f in data.files))
            # Mark that the first layout after this batch should seed the
            # heatmap immediately.  We cannot call _prioritize_visible_thumbnails
            # here because _visible_to_original_mapping is not yet populated —
            # label creation and layout update happen asynchronously via timers.
            if first_batch:
                self._needs_heatmap_seed = True
        except Exception as e:
            # why: protocol extensions in future daemon versions may produce
            # unexpected field types; isolate to prevent notification loop crash.
            logger.error("Unexpected exception in scan_progress handler: %s", e, exc_info=True)

    @Slot(object)
    def _on_files_removed(self, data: FilesRemovedData) -> None:
        logger.debug("[trace] files_removed: all_files=%d, is_loading=%s", len(self.all_files), self._is_loading)
        if data.files:
            logger.info("Removing %d ghost files from view.", len(data.files))
            self.remove_images([f.path for f in data.files])

    @Slot(object)
    def _on_scan_complete(self, data: ScanCompleteData) -> None:
        logger.debug("[trace] scan_complete: all_files=%d, is_loading=%s", len(self.all_files), self._is_loading)
        if self._startup_t0 is not None:
            elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
            logger.info("[startup] scan_complete: %.0f ms after load_directory", elapsed_ms)
        logger.info(
            "[virtual] scan_complete: all_files=%d, labels=%d, current_files(in layout)=%d",
            len(self.all_files), len(self.labels), len(self.current_files),
        )
        self._filter_update_timer.stop()
        self._scan_coalesce_timer.stop()
        self._scan_active = False
        self._is_loading = False
        self._scan_batch_pending = False

        if not self._hidden_indices and self._virtual_grid:
            # No filter active: data structures are fully populated by the
            # append-only fast path.  Do one final sorted reorder (no-op if
            # all_files is already sorted) and snap the container height.
            top_file = self._get_first_visible_file()
            logger.info(
                "[virtual] scan_complete: final sort, top_file=%s, all_files=%d",
                os.path.basename(top_file) if top_file else None, len(self.all_files),
            )
            self._virtual_grid.snap_height_to_exact()
            self.reorder_files(sorted(self.all_files))
            if top_file:
                self.scroll_to_top(top_file)
                self._sync_virtual_viewport()
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)
        else:
            # Filter active: full rebuild unavoidable.
            self.reapply_filters()

    def _add_image_batch(self, files: List[str]):
        """Adds a batch of new file paths (data-only, no widget creation).

        Index bookkeeping (all_files, _all_files_set, _path_to_idx,
        image_states) is done immediately.  Cached inline thumbnails are
        loaded into _pixmap_cache.  Widget creation is deferred to
        _sync_virtual_viewport via the filter/layout pipeline.
        """
        if not files:
            return

        new_files = [f for f in files if f not in self._all_files_set]
        if not new_files:
            logger.debug("[virtual] _add_image_batch: all %d files already known, skipping", len(files))
            return

        start_idx = len(self.all_files)
        self.all_files.extend(new_files)
        self._all_files_set.update(new_files)
        for i, f in enumerate(new_files):
            orig_idx = start_idx + i
            self._path_to_idx[f] = orig_idx
            self.image_states[orig_idx] = ImageState()

            # Store cached inline thumbnail paths for lazy loading
            if f in self._initial_thumb_paths:
                thumb_path = self._initial_thumb_paths.pop(f)
                self._thumb_path_cache[orig_idx] = thumb_path

        logger.info(
            "[virtual] _add_image_batch: +%d new files (all_files=%d)",
            len(new_files), len(self.all_files),
        )
        if self._group_mode:
            self._rebuild_group_map()
        if not self._hidden_indices and not self._group_mode:
            if self._scan_active:
                # Active scan (cached or uncached): append new files to end.
                # Existing items never shift — only the scrollbar range grows.
                # Layout update is coalesced via timer to avoid per-batch jitter.
                for f in new_files:
                    vis_idx = len(self.current_files)
                    orig_idx = self._path_to_idx[f]
                    self.current_files.append(f)
                    self._visible_to_original_mapping[vis_idx] = orig_idx
                    self._original_to_visible_mapping[orig_idx] = vis_idx
                    self._visible_original_indices.append(orig_idx)
                self._scan_batch_pending = True
                logger.debug(
                    "[virtual] _scan_active append: +%d files (current_files=%d, coalesce_active=%s)",
                    len(new_files), len(self.current_files), self._scan_coalesce_timer.isActive(),
                )
                if not self._scan_first_batch_flushed:
                    # First batch: flush immediately so the initial layout
                    # appears and heatmap seeding can fire.
                    self._scan_coalesce_timer.stop()
                    self._flush_scan_layout()
                elif not self._scan_coalesce_timer.isActive():
                    # Don't restart if already running — let it fire on
                    # schedule so flushes happen every ~250ms during
                    # continuous scanning rather than being pushed out
                    # indefinitely.
                    self._scan_coalesce_timer.start()
            else:
                # Post-scan arrival (watchdog, ComfyUI): insert in sorted
                # position so new files appear next to related originals.
                if len(new_files) > 50:
                    self.reorder_files(sorted(self.all_files))
                else:
                    self._insert_sorted_incremental(new_files)
        else:
            # Filter is active — full rebuild needed to check which new
            # files pass the filter.
            if self._is_loading:
                self._apply_filter_results(set(self.all_files))
            else:
                self._filter_update_timer.start()

    def _flush_scan_layout(self):
        """Coalesced layout update during scan."""
        if not self._scan_batch_pending or not self._virtual_grid:
            return
        self._scan_batch_pending = False
        self._scan_first_batch_flushed = True
        logger.info(
            "[virtual] _flush_scan_layout: current_files=%d, all_files=%d",
            len(self.current_files), len(self.all_files),
        )
        self._virtual_grid.set_total_items_chunked(len(self.current_files))
        self._virtual_grid.update_layout()
        self._sync_virtual_viewport()
        self._last_layout_file_count = len(self.all_files)

    def _insert_sorted_incremental(self, new_files: list):
        """Insert new files at sorted positions without full rebuild.

        Precondition: new_files already appended to all_files and indexed in
        _path_to_idx.  current_files is maintained sorted.
        """
        import bisect

        for f in sorted(new_files):
            orig_idx = self._path_to_idx[f]
            insert_pos = bisect.bisect_left(self.current_files, f)
            self.current_files.insert(insert_pos, f)
            self._visible_original_indices.insert(insert_pos, orig_idx)

        # Rebuild visible<->original mappings (O(n) but no label recycling)
        self._visible_to_original_mapping.clear()
        self._original_to_visible_mapping.clear()
        for vis_idx, orig_idx in enumerate(self._visible_original_indices):
            self._visible_to_original_mapping[vis_idx] = orig_idx
            self._original_to_visible_mapping[orig_idx] = vis_idx

        if self._virtual_grid:
            mapping = self._original_to_visible_mapping
            self._virtual_grid.reindex_labels(
                len(self.current_files),
                lambda label: mapping.get(label._original_idx),
            )
            self._sync_virtual_viewport()

        self._last_layout_file_count = len(self.all_files)
        QTimer.singleShot(100, self._prioritize_visible_thumbnails)

    def add_images(self, image_paths: List[str]) -> None:
        normalized = [os.path.abspath(p) for p in image_paths]
        self._add_image_batch(normalized)

    def remove_images(self, paths: List[str]):
        if not paths:
            return

        self._benchmark_timer.start()

        try:
            paths_set = set(paths)

            # -- 1. Capture which visible indices are being removed ----------
            removed_vis_indices: set = set()
            for p in paths:
                orig = self._path_to_idx.get(p)
                if orig is not None:
                    vis = self._original_to_visible_mapping.get(orig)
                    if vis is not None:
                        removed_vis_indices.add(vis)

            # -- 2. Snapshot old hidden paths before index shift -------------
            old_hidden_paths = {self.all_files[i] for i in self._hidden_indices
                                if i < len(self.all_files)}

            # -- 3. Rebuild data arrays with new indices (same as before) ----
            new_all_files = []
            new_image_states = {}
            new_pixmap_cache = {}
            new_thumb_path_cache = {}

            current_new_idx = 0
            for original_idx, file_path in enumerate(self.all_files):
                if file_path not in paths_set:
                    new_all_files.append(file_path)
                    if original_idx in self.image_states:
                        new_image_states[current_new_idx] = self.image_states[original_idx]
                    if original_idx in self._pixmap_cache:
                        new_pixmap_cache[current_new_idx] = self._pixmap_cache[original_idx]
                    if original_idx in self._thumb_path_cache:
                        new_thumb_path_cache[current_new_idx] = self._thumb_path_cache[original_idx]
                    current_new_idx += 1

            self.all_files = new_all_files
            self._all_files_set = set(new_all_files)
            self._path_to_idx = {path: idx for idx, path in enumerate(new_all_files)}
            self.image_states = new_image_states
            self._pixmap_cache = new_pixmap_cache
            self._thumb_path_cache = new_thumb_path_cache

            # -- 4a. Rebuild group map if group mode is active ----------------
            if self._group_mode:
                self._rebuild_group_map()

            # -- 4b. Shift _hidden_indices to match new original indices ------
            #
            # Filter-hidden paths come from old_hidden_paths (excluding any
            # that were group-hidden — those are rebuilt from scratch below).
            # Group-hidden paths come purely from the freshly rebuilt group map
            # so that dissolving a group (e.g. JPG primary deleted) correctly
            # un-hides the orphaned RAW.
            self._hidden_indices = set()
            for hp in old_hidden_paths:
                if self._group_mode and hp in self._raw_to_primary:
                    continue  # handled by group map below
                new_idx = self._path_to_idx.get(hp)
                if new_idx is not None:
                    self._hidden_indices.add(new_idx)
            if self._group_mode:
                for raw_path in self._grouped_raw_paths:
                    idx = self._path_to_idx.get(raw_path)
                    if idx is not None:
                        self._hidden_indices.add(idx)

            # -- 5. Rebuild visible ↔ original mappings inline ---------------
            self.current_files = []
            self._visible_to_original_mapping.clear()
            self._original_to_visible_mapping.clear()
            self._visible_original_indices.clear()

            visible_idx = 0
            for orig_idx, file_path in enumerate(self.all_files):
                if orig_idx not in self._hidden_indices:
                    self.current_files.append(file_path)
                    self._visible_to_original_mapping[visible_idx] = orig_idx
                    self._original_to_visible_mapping[orig_idx] = visible_idx
                    self._visible_original_indices.append(orig_idx)
                    visible_idx += 1

            # -- 6. Surgical grid splice — only recycle deleted labels -------
            if self._virtual_grid and removed_vis_indices:
                vis_to_orig = self._visible_to_original_mapping

                def _update_label(label, new_vis_idx):
                    new_orig = vis_to_orig[new_vis_idx]
                    label._original_idx = new_orig
                    label.file_path = self.all_files[new_orig]

                self._virtual_grid.splice_items(
                    removed_vis_indices,
                    len(self.current_files),
                    self._recycle_label,
                    _update_label,
                )

                # Re-key self.labels from old original_idx → new original_idx.
                new_labels = {}
                for label in self._virtual_grid._mat_labels.values():
                    new_labels[label._original_idx] = label
                self.labels = new_labels
            elif self._virtual_grid:
                # Nothing visible was removed (all removed items were hidden).
                self._virtual_grid.set_total_items(len(self.current_files))

            # -- 7. Clear hover if the hovered label was deleted -------------
            if self._hovered_label is not None:
                if self._hovered_label.file_path in paths_set:
                    self._hovered_label = None
                    self.thumbnailLeft.emit()

            # -- 8. Preserve selection of surviving files ----------------------
            surviving_selection = self._current_selection - paths_set
            if surviving_selection != self._current_selection:
                cmd = ReplaceSelectionCommand(paths=surviving_selection, source="thumbnail_view", timestamp=time.time())
                event_system.publish(cmd)
            self._recompute_selected_indices()

            # -- 9. Fill any new gaps at viewport edges ----------------------
            self._last_layout_file_count = len(self.all_files)
            self._sync_virtual_viewport()
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)

            self._last_redraw_time = self._benchmark_timer.elapsed() / 1000.0
            self.benchmarkComplete.emit("Redraw", self._last_redraw_time)

        except (KeyError, IndexError) as e:
            # why: index/path maps can desync if a watchdog removal races with an
            # in-progress remove_images call on the same set of paths.
            logger.error("Error removing images: %s", e, exc_info=True)

    def reorder_files(self, ordered_paths: list):
        """Reorder all_files to match *ordered_paths* and refresh the layout.

        Unknown paths in *ordered_paths* are silently dropped.
        """
        old_idx_map = {path: idx for idx, path in enumerate(self.all_files)}
        new_all_files = [p for p in ordered_paths if p in old_idx_map]

        if not new_all_files or new_all_files == self.all_files:
            return

        new_image_states = {}
        new_pixmap_cache = {}
        new_thumb_path_cache = {}
        for new_idx, path in enumerate(new_all_files):
            old_idx = old_idx_map[path]
            if old_idx in self.image_states:
                new_image_states[new_idx] = self.image_states[old_idx]
            if old_idx in self._pixmap_cache:
                new_pixmap_cache[new_idx] = self._pixmap_cache[old_idx]
            if old_idx in self._thumb_path_cache:
                new_thumb_path_cache[new_idx] = self._thumb_path_cache[old_idx]

        # Recycle all materialized labels — re-materialized with new indices
        if self._virtual_grid:
            self._virtual_grid.clear(self._recycle_label)
        self.labels.clear()

        self.all_files = new_all_files
        self._all_files_set = set(new_all_files)
        self._path_to_idx = {path: idx for idx, path in enumerate(new_all_files)}
        self.image_states = new_image_states
        self._pixmap_cache = new_pixmap_cache
        self._thumb_path_cache = new_thumb_path_cache
        self._recompute_selected_indices()

        self._update_filtered_layout()

    def scroll_to_top(self, image_path: str) -> None:
        """Scroll so the row containing *image_path* is the first visible row,
        then materialize widgets.
        """
        original_idx = self._path_to_idx.get(image_path, -1)
        if original_idx < 0:
            return
        visible_idx = self._original_to_visible_mapping.get(original_idx)
        if visible_idx is None:
            return
        if self._virtual_grid:
            self._virtual_grid.scroll_to_top(visible_idx)
            self._sync_virtual_viewport()

    def invalidate_thumbnails(self, image_paths: List[str], clear_labels: bool = True) -> None:
        """Evict cached thumbnail state for the given paths so fresh
        thumbnails are accepted when the next ``previews_ready`` arrives.

        When *clear_labels* is True (default), materialized labels are reset
        to placeholder immediately.  When False, the old pixmap stays visible
        until the replacement ``previews_ready`` arrives — avoiding a flash.
        """
        for path in image_paths:
            idx = self._path_to_idx.get(path)
            if idx is None:
                continue
            state = self.image_states.get(idx)
            if state:
                state.loaded = False
                state.prioritized = False
            self._pixmap_cache.pop(idx, None)
            self._thumb_path_cache.pop(idx, None)
            self._last_thumb_pairs.pop(path, None)
            if clear_labels:
                label = self.labels.get(idx)
                if label:
                    label.clear()
                    label.loaded = False

    def _apply_db_orientation(self, image: QImage, image_path: str) -> QImage:
        if not self.service:
            return image
        try:
            resp = self.service.get_metadata_batch([image_path])
            orientation = resp.get(image_path, {}).get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            return image
        degrees = PictureBase.EXIF_ORIENTATION_DEGREES.get(orientation, 0)
        if degrees:
            return image.transformed(QTransform().rotate(degrees), Qt.SmoothTransformation)
        return image

    def rotate_thumbnails(self, image_paths: List[str], degrees: int) -> None:
        # why: optimistic rotation for immediate feedback before regenerated thumbnail arrives
        transform = QTransform().rotate(degrees)
        for path in image_paths:
            idx = self._path_to_idx.get(path)
            if idx is None:
                continue
            pixmap = self._pixmap_cache.get(idx)
            if pixmap and not pixmap.isNull():
                rotated = pixmap.transformed(transform, Qt.SmoothTransformation)
                scaled = rotated.scaled(self.display_size, self.display_size,
                                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._pixmap_cache[idx] = scaled
                label = self.labels.get(idx)
                if label:
                    label.updateThumbnail(scaled)
            # Clear so _tick_preview_loading accepts the regenerated thumbnail.
            self._thumb_path_cache.pop(idx, None)
            self._last_thumb_pairs.pop(path, None)

    def ensure_visible(self, original_idx: int, center: bool = False):
        """Scroll so the thumbnail at *original_idx* is in the viewport, then
        materialize widgets so it actually appears.
        """
        visible_idx = self._original_to_visible_mapping.get(original_idx)
        if visible_idx is None:
            logger.debug("Original index %d not visible (filtered out)", original_idx)
            return

        if self._virtual_grid:
            self._virtual_grid.ensure_visible(visible_idx, center)
            self._sync_virtual_viewport()

    def closeEvent(self, event):
        event_system.unsubscribe(EventType.SELECTION_CHANGED, self._on_selection_changed)
        event_system.unsubscribe(EventType.SHIFT_PRESSED, self._on_shift_pressed)
        event_system.unsubscribe(EventType.SHIFT_RELEASED, self._on_shift_released)
        event_system.unsubscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
        event_system.unsubscribe(EventType.FACE_PERSON_FILTER, self._on_face_person_filter)
        event_system.unsubscribe(EventType.TEXT_FILTER_CHANGED, self._on_text_filter_event)
        event_system.unsubscribe(EventType.STAR_FILTER_CHANGED, self._on_star_filter_event)
        event_system.unsubscribe(EventType.TAG_FILTER_CHANGED, self._on_tag_filter_event)
        event_system.unsubscribe(EventType.CLEAR_FILTERS, self._on_clear_filters_event)
        if self._daemon_signals:
            self._daemon_signals.previews_ready.disconnect(self._on_previews_ready)
            self._daemon_signals.scan_progress.disconnect(self._on_scan_progress)
            self._daemon_signals.files_removed.disconnect(self._on_files_removed)
            self._daemon_signals.scan_complete.disconnect(self._on_scan_complete)

        # Stop timers
        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_filter_update_timer'):
            self._filter_update_timer.stop()
        if hasattr(self, '_priority_update_timer'):
            self._priority_update_timer.stop()
        if hasattr(self, '_preview_tick_timer'):
            self._preview_tick_timer.stop()
        if hasattr(self, '_viewport_executor'):
            # wait=False: in-flight viewport calls are best-effort priority hints; the
            # socket client handles broken-pipe errors on its own after widget teardown.
            self._viewport_executor.shutdown(wait=False)

        # Clear pixmap/path caches
        self._pixmap_cache.clear()
        self._thumb_path_cache.clear()

        # Clear widget pool
        for label in self._widget_pool:
            label.deleteLater()
        self._widget_pool.clear()

        super().closeEvent(event)

    def clear_layout(self):
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
        if hasattr(self, '_scan_coalesce_timer'):
            self._scan_coalesce_timer.stop()
            self._scan_batch_pending = False
            self._scan_active = False
        # Recycle all materialized labels via VirtualGridManager
        if hasattr(self, '_virtual_grid') and self._virtual_grid:
            self._virtual_grid.clear(self._recycle_label)

        self.labels.clear()
        cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
        event_system.publish(cmd)
        self.selection_anchor_index = None
        self._selected_indices.clear()
        self._last_preview_selected.clear()

        self.pending_thumbnails.clear()
        self._pixmap_cache.clear()
        self._thumb_path_cache.clear()
        self._initial_thumb_paths.clear()
        self.current_files.clear()
        self.all_files.clear()
        self._all_files_set.clear()
        self._path_to_idx.clear()
        self.current_directory_path = None
        self.image_states.clear()
        self._folder_nodes.clear()
        self._navigation_stack.clear()
        self._last_layout_file_count = 0
        # Cancel any in-flight speculative fullres tasks from the old directory
        # so workers don't waste time on files no longer visible.
        if self._last_fullres_pairs and self.service:
            paths_to_cancel = list(self._last_fullres_pairs.keys())
            self._viewport_executor.submit(
                self.service.update_viewport_heatmap,
                [], [], [], paths_to_cancel,
            )
        self._last_thumb_pairs = {}
        self._last_fullres_pairs = {}
        self._stale_request_ts = {}
        self._hovered_label = None

        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_filter_update_timer'):
            self._filter_update_timer.stop()

    def _on_thumbnail_ready(self, original_path: str, image: Optional[QImage], error: Optional[Exception]):
        """Handles thumbnail generation results in the main GUI thread.

        Always stores the pixmap in _pixmap_cache.  If the label is currently
        materialized (visible), updates it immediately.
        """
        is_error = error or image is None or image.isNull()
        if is_error:
            _err_img = QImage(self.display_size, self.display_size, QImage.Format_RGB32)
            _err_img.fill(QColor(255, 0, 0))
            pixmap = QPixmap.fromImage(_err_img)
        else:
            pixmap = apply_profile_pixmap(image)

        if is_error:
            logger.error("Thumbnail generation failed for %s", original_path, exc_info=bool(error))

        original_idx = self._path_to_idx.get(original_path, -1)
        if original_idx >= 0:
            # Always cache the pixmap (even errors, so we don't re-request)
            self._pixmap_cache[original_idx] = pixmap
            state = self.image_states.get(original_idx)
            if state:
                state.loaded = not is_error
                state.prioritized = False
            # Update materialized label if present
            label = self.labels.get(original_idx)
            if label:
                label.updateThumbnail(pixmap)
                logger.debug("[thumb] applied thumbnail for %s (error=%s)", os.path.basename(original_path), is_error)

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
                # Already loaded — but accept if the path changed (rotation/edit
                # regenerated the thumbnail at a new cache path).
                if self._thumb_path_cache.get(orig_idx) == thumbnail_path:
                    continue
            image = QImage(thumbnail_path)
            if not image.isNull():
                image = self._apply_db_orientation(image, image_path)
                self._thumb_path_cache[orig_idx] = thumbnail_path
                self._thumbnail_generated_signal.emit(image_path, image, None)
            else:
                logger.warning("Failed to load thumbnail: %s", thumbnail_path)

        if not self._pending_previews:
            self._preview_tick_timer.stop()
            if self._startup_t0 is not None and not self._startup_thumbnails_emitted:
                self._startup_thumbnails_emitted = True
                elapsed_ms = (time.perf_counter() - self._startup_t0) * 1000
                logger.info("[startup] initial thumbnails drawn: %.0f ms after load_directory", elapsed_ms)

    def get_benchmark_results(self) -> dict:
        return {
            "Initial Load Time": self._last_load_time,
            "Redraw Time": self._last_redraw_time,
            "Total Images": len(self.current_files),
            "Cached Images": len(self._pixmap_cache),
            "Pending Images": len(self.pending_thumbnails)
        }

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_start_index != -1:
            if self._drag_shift_pending:
                self._range_selection_active = True
                self.selection_anchor_index = self._drag_start_index
                self._selection_mode = "add"
                self.setCursor(Qt.CrossCursor)
                self._drag_start_index = -1
                self._drag_last_index = -1
                self._drag_shift_pending = False
            else:
                self._commit_selection(self._drag_start_index, self._drag_last_index)
                self._drag_start_index = -1
                self._drag_last_index = -1
                self._selection_mode = None
                self._last_preview_selected = set()

        super().mouseReleaseEvent(event)

    def _restore_pre_click_selection(self):
        """Restore selection to the state before the last mouse press (used by double-click)."""
        pre = getattr(self, '_pre_click_selection', None)
        if pre is not None:
            cmd = ReplaceSelectionCommand(paths=pre, source="thumbnail_view", timestamp=time.time())
            event_system.publish(cmd)
            # Reset drag state so the release doesn't re-commit
            self._drag_start_index = -1
            self._drag_last_index = -1
            self._selection_mode = None
            self._last_preview_selected = set()

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
        paths_in_range = self._get_paths_in_range(start_idx, end_idx)
        command = None

        if self._selection_mode == "replace":
            # Single-click on an already-selected image with no modifiers clears selection
            if start_idx == end_idx and paths_in_range <= self._current_selection:
                paths_in_range = set()
            command = ReplaceSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "add":
            command = AddToSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "remove":
            command = RemoveFromSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())

        if command:
            event_system.publish(command)
            self.selection_anchor_index = end_idx

    def _get_indices_in_range(self, start_idx: int, end_idx: int) -> Set[int]:
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
                self._restore_pre_click_selection()
                self._emit_double_click(hovered_path)
                logger.debug("Double-clicked on thumbnail, emitting signal for path: %s", hovered_path)
            else:
                logger.debug("Double-click, but no image path hovered.")

    def _emit_double_click(self, path: str):
        """Route double-click to folder navigation or image open."""
        if path in self._folder_nodes:
            self.folderNavigated.emit(path)
        else:
            self.doubleClicked.emit(path)

    def navigate_to_folder(self, folder_path: str):
        """Navigate into a subdirectory, pushing current path onto the stack."""
        if self.current_directory_path:
            self._navigation_stack.append(self.current_directory_path)
        self.load_directory(folder_path, recursive=False)

    def navigate_to_parent(self):
        """Navigate back to the previous directory from the stack."""
        if self._navigation_stack:
            parent = self._navigation_stack.pop()
            self.load_directory(parent, recursive=False)

    def setHighlightedThumbnail(self, image_path: str):
        """Briefly highlight a thumbnail on return from picture view without changing selection."""
        try:
            original_idx = self._path_to_idx.get(image_path, -1)
            if original_idx < 0:
                logger.warning("Image %s not found in all_files during highlight attempt.", image_path)
                return

            if original_idx in self._original_to_visible_mapping:
                label_to_highlight = self.labels.get(original_idx)
                if label_to_highlight:
                    label_to_highlight.setSelected(True)
                    self.ensure_visible(original_idx, center=True)
                    QTimer.singleShot(1000, lambda: label_to_highlight.setSelected(False))
                else:
                    logger.debug("Label for original index %d not found.", original_idx)
            else:
                logger.debug("Image %s (original index %d) not currently visible.", image_path, original_idx)

        except (AttributeError, RuntimeError) as e:
            # why: label or scroll bar can be partially torn down if a directory
            # reload races with the highlight timer firing.
            logger.error("Error highlighting thumbnail: %s", e, exc_info=True)

    def _get_thumbnail_at_pos(self, pos: QPoint) -> Optional[int]:
        if not self._virtual_grid:
            return None

        # Convert position from this widget's coordinates to the scroll area's viewport
        global_pos = self.mapToGlobal(pos)
        pos_in_viewport = self.scroll_area.viewport().mapFromGlobal(global_pos)

        # Adjust for scroll position to get point in grid container coords
        h_scroll = self.scroll_area.horizontalScrollBar().value()
        v_scroll = self.scroll_area.verticalScrollBar().value()
        pos_in_container = pos_in_viewport + QPoint(h_scroll, v_scroll)

        visible_idx = self._virtual_grid.get_widget_at_position(pos_in_container)

        if visible_idx is not None:
            return self._visible_to_original_mapping.get(visible_idx)

        return None

    def apply_filter(self, filter_text: str):
        self._current_filter = filter_text
        self._filter_update_timer.start()

    def apply_star_filter(self, star_states: list):
        self._current_star_filter = star_states
        self._filter_update_timer.start()

    def apply_tag_filter(self, tag_names: list):
        self._current_tag_filter = list(tag_names)
        self._filter_update_timer.start()

    def clear_filter(self):
        self._current_filter = ""
        self._current_star_filter = [True, True, True, True, True, True]
        self._current_tag_filter = []
        self._clip_search_paths = None
        self._person_filter_paths = None
        self._hidden_indices = set()
        self._filter_update_timer.start()

    def apply_clip_search_results(self, result_paths: list):
        """Set CLIP search filter and reapply all filters."""
        self._clip_search_paths = set(result_paths)
        self._filter_update_timer.start()

    def clear_clip_search(self):
        """Remove CLIP search filter and reapply all filters immediately."""
        self._clip_search_paths = None
        self._hidden_indices = set()
        self.reapply_filters()

    def apply_person_filter(self, file_paths: list):
        self._person_filter_paths = set(file_paths)
        self._filter_update_timer.start()

    def clear_person_filter(self):
        self._person_filter_paths = None
        self.reapply_filters()

    def _on_face_person_filter(self, event_data):
        person_ids = event_data.person_ids
        if not person_ids:
            self.clear_person_filter()
            return
        if not self.service:
            return
        file_paths = self.service.get_face_paths_for_persons(person_ids)
        self.apply_person_filter(file_paths or [])

    def _on_text_filter_event(self, event_data):
        self.apply_filter(event_data.filter_text)

    def _on_star_filter_event(self, event_data):
        self.apply_star_filter(event_data.star_states)

    def _on_tag_filter_event(self, event_data):
        self.apply_tag_filter(event_data.tag_names)

    def _on_clear_filters_event(self, event_data):
        self.clear_filter()

    def navigate_to_file(self, file_path: str):
        """Scroll to and highlight a file (used by CLIP search result selection)."""
        if file_path in self.current_files:
            self.setHighlightedThumbnail(file_path)

    def reapply_filters(self):
        """
        Re-applies all active filters.  When the scan is still running the
        filter is computed locally (fast path).  Otherwise the daemon is
        queried on a background thread so the GUI never blocks on I/O.
        """
        logger.info(
            "[virtual] reapply_filters: is_loading=%s, all_files=%d, labels=%d",
            self._is_loading, len(self.all_files), len(self.labels),
        )

        if not self.all_files or not self.service:
            logger.warning("Cannot apply filters: file list or service is not ready.")
            return

        if self._is_loading:
            # Fast path: show everything during the initial scan.
            visible = set(self.all_files)
            if self._clip_search_paths is not None:
                visible = visible & self._clip_search_paths
            if self._person_filter_paths is not None:
                visible = visible & self._person_filter_paths
            self._apply_filter_results(visible)
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
        tag_filter = list(self._current_tag_filter)
        self._viewport_executor.submit(self._fetch_filtered_paths, text_filter, star_filter, tag_filter)

    def _fetch_filtered_paths(self, text_filter: str, star_filter: list, tag_filter: list):
        """Runs on _viewport_executor thread — never blocks the GUI."""
        try:
            response = self.service.get_filtered_file_paths(
                text_filter, star_filter, tag_names=tag_filter or None
            )
            if response is not None:
                self._filtered_paths_ready.emit(set(response))
            else:
                logger.error("Failed to get filtered paths from daemon.")
                self._filtered_paths_ready.emit(None)
        except Exception as e:
            # why: service calls can raise; broad guard ensures
            # _filtered_paths_ready always fires to unlock _filter_in_flight.
            logger.error("Error fetching filtered paths: %s", e, exc_info=True)
            self._filtered_paths_ready.emit(None)

    @Slot(object)
    def _on_filtered_paths_ready(self, visible_paths):
        self._filter_in_flight = False

        if visible_paths is None:
            visible_paths = set(self.all_files)

        # Intersect with CLIP search results when active
        if self._clip_search_paths is not None:
            visible_paths = visible_paths & self._clip_search_paths
        if self._person_filter_paths is not None:
            visible_paths = visible_paths & self._person_filter_paths

        self._apply_filter_results(visible_paths)

        # If another filter change arrived while this one was in flight,
        # re-submit with the latest filter values.
        if self._filter_pending:
            self._filter_pending = False
            self.reapply_filters()

    def _apply_filter_results(self, visible_paths: set):
        # Folders are always visible regardless of filters
        if self._folder_nodes:
            visible_paths = visible_paths | self._folder_nodes.keys()
        new_hidden_indices = set()
        for i, file_path in enumerate(self.all_files):
            if file_path not in visible_paths:
                new_hidden_indices.add(i)
            elif self._group_mode and file_path in self._grouped_raw_paths:
                new_hidden_indices.add(i)

        hidden_changed = self._hidden_indices != new_hidden_indices
        count_changed = len(self.all_files) != self._last_layout_file_count
        will_update = hidden_changed or count_changed

        logger.info(
            "[virtual] _apply_filter_results: all_files=%d, visible_paths=%d, "
            "hidden=%d, hidden_changed=%s, count_changed=%s, will_update=%s",
            len(self.all_files), len(visible_paths), len(new_hidden_indices),
            hidden_changed, count_changed, will_update,
        )

        if will_update:
            self._hidden_indices = new_hidden_indices
            self._update_filtered_layout()
            self._last_layout_file_count = len(self.all_files)
            logger.info(
                "[virtual] _update_filtered_layout done: current_files=%d, materialized_labels=%d",
                len(self.current_files), len(self.labels),
            )
        else:
            total_count = len(self.all_files)
            visible_count = len(self.current_files)
            hidden_count = total_count - visible_count

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
        """Rebuild visible-index mappings (pure Python, no Qt calls) and sync viewport."""
        if not self._virtual_grid:
            return

        self.current_files = []
        self._visible_to_original_mapping.clear()
        self._original_to_visible_mapping.clear()
        self._visible_original_indices.clear()

        visible_idx = 0
        for original_idx, file_path in enumerate(self.all_files):
            if original_idx not in self._hidden_indices:
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

        # Recycle all materialized labels then re-materialize for the new mapping
        self._virtual_grid.clear(self._recycle_label)
        self.labels.clear()
        self._virtual_grid.set_total_items(len(self.current_files))
        self._virtual_grid.update_layout()
        self._sync_virtual_viewport()

        if self._needs_heatmap_seed:
            self._needs_heatmap_seed = False
            self._prioritize_visible_thumbnails()
        else:
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)

    def _sync_virtual_viewport(self):
        if self._virtual_grid and self.current_files:
            self._virtual_grid.sync_viewport(
                self._materialize_label,
                self._recycle_virtual_label,
            )

    def _get_first_visible_file(self) -> Optional[str]:
        """Return the file path at the top-left of the visible viewport."""
        if not self._virtual_grid or not self.current_files:
            return None
        first_row, _ = self._virtual_grid.get_visible_rows()
        first_vis_idx = first_row * self._virtual_grid.columns
        if 0 <= first_vis_idx < len(self.current_files):
            return self.current_files[first_vis_idx]
        return None

    def _materialize_label(self, visible_idx: int) -> ThumbnailLabel:
        original_idx = self._visible_to_original_mapping[visible_idx]
        file_path = self.all_files[original_idx]

        label = self._get_or_create_label(file_path, original_idx)
        label._original_idx = original_idx
        label._overlay_manager = self.overlay_manager

        # Configure folder cards
        if file_path in self._folder_nodes:
            was_folder = getattr(label, 'is_folder', False)
            label.is_folder = True
            label._folder_node = self._folder_nodes.get(file_path)
            label._cache_folder_previews()
            label.loaded = True  # folders don't need thumbnail generation
            state = self.image_states.get(original_idx)
            if state:
                state.loaded = True
            if not was_folder:
                label.setStyleSheet(label._build_card_stylesheet())
        else:
            if getattr(label, 'is_folder', False):
                label.setStyleSheet(label._build_card_stylesheet())
            label.is_folder = False
            label._folder_node = None
            label._folder_preview_pixmaps = None
            # Apply cached pixmap (lazy-load from thumb path if needed)
            pixmap = self._pixmap_cache.get(original_idx)
            if pixmap is None:
                thumb_path = self._thumb_path_cache.get(original_idx)
                if thumb_path:
                    image = QImage(thumb_path)
                    if not image.isNull():
                        image = self._apply_db_orientation(image, file_path)
                        pixmap = apply_profile_pixmap(image)
                        self._pixmap_cache[original_idx] = pixmap
            if pixmap:
                label.updateThumbnail(pixmap)
                label.loaded = True
                state = self.image_states.get(original_idx)
                if state:
                    state.loaded = True

        # Apply selection state — during an active drag, use the preview set
        # so recycled labels re-appear with the correct highlight.
        if self._drag_start_index != -1 and self._last_preview_selected:
            label.setSelected(original_idx in self._last_preview_selected)
        else:
            label.setSelected(original_idx in self._selected_indices)

        self.labels[original_idx] = label
        return label

    def _recycle_virtual_label(self, label: ThumbnailLabel):
        orig_idx = label._original_idx
        self.labels.pop(orig_idx, None)
        self._recycle_label(label)

    def _on_scroll(self, value):
        """Slot to handle scroll bar value changes.

        Starts a repeating heatmap timer so thumbnails update continuously
        during scrolling.  A separate idle timer stops the repeating timer
        200ms after the last scroll event.
        """
        self._sync_virtual_viewport()  # materialize/recycle labels for new scroll pos
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

        For cached/post-scan folders the full visible viewport is requested
        at GUI_REQUEST_LOW.
        """
        if not self.service or not self.labels or not self.current_files or not self._virtual_grid:
            return

        columns = self._virtual_grid.columns
        if columns <= 0:
            return

        # --- Determine heatmap center ---
        ref_visible_idx = None
        if self._hovered_label:
            hovered_orig_idx = self._label_to_original_idx(self._hovered_label)
            if hovered_orig_idx is not None:
                ref_visible_idx = self._original_to_visible_mapping.get(hovered_orig_idx)

        if ref_visible_idx is None:
            first_row, last_row = self._virtual_grid.get_visible_rows()
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
        folder_paths = self._folder_nodes
        for vis_idx, priority in thumb_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                path = all_files[orig_idx]
                if path not in folder_paths:
                    current_thumb[path] = priority

        # --- Cached/post-scan: request full viewport at GUI_REQUEST_LOW ---
        # When no scan is competing for workers, aggressively request the
        # entire visible viewport so cached thumbnails appear immediately.
        if not self._is_loading:
            first_row, last_row = self._virtual_grid.get_visible_rows()
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
                if path not in current_thumb and path not in folder_paths:
                    current_thumb[path] = int(Priority.GUI_REQUEST_LOW)

        current_fullres: dict[str, int] = {}
        for vis_idx, priority in fullres_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                current_fullres[all_files[orig_idx]] = priority

        # --- Early out: skip IPC when every (path, priority) pair is identical ---
        if current_thumb == self._last_thumb_pairs and current_fullres == self._last_fullres_pairs:
            # Check for stale requests: paths we already sent but never got a
            # notification back for (e.g. notification channel dropped).
            now = time.monotonic()
            stale_evicted = 0
            for p in list(current_thumb):
                oi = self._path_to_idx.get(p, -1)
                if oi < 0:
                    continue
                st = self.image_states.get(oi)
                if st and not st.loaded:
                    first_seen = self._stale_request_ts.get(p)
                    if first_seen is None:
                        self._stale_request_ts[p] = now
                    elif now - first_seen >= self._STALE_THRESHOLD_S:
                        # Evict so the delta logic below re-requests it.
                        del self._last_thumb_pairs[p]
                        del self._stale_request_ts[p]
                        stale_evicted += 1
                else:
                    # Loaded — no longer stale.
                    self._stale_request_ts.pop(p, None)
            if stale_evicted:
                logger.info(
                    "[heatmap] re-requesting %d stale thumbnails", stale_evicted,
                )
                # Fall through to delta computation with the evicted entries.
            else:
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
            logger.debug(
                "[trace] heatmap IPC: upgrades=%d, downgrades=%d, fullres=%d, "
                "fullres_cancel=%d, is_loading=%s",
                len(delta_upgrade), len(paths_to_downgrade), len(delta_fullres),
                len(fullres_to_cancel), self._is_loading,
            )
            self._viewport_generation += 1
            gen = self._viewport_generation
            def _send_if_current(generation, up, down, fr, fc):
                if self._viewport_generation != generation:
                    return
                self.service.update_viewport_heatmap(up, down, fr, fc)

            self._viewport_executor.submit(
                _send_if_current, gen,
                delta_upgrade, paths_to_downgrade,
                delta_fullres, fullres_to_cancel,
            )

        self._last_thumb_pairs = current_thumb
        self._last_fullres_pairs = current_fullres

    def get_visible_count(self) -> int:
        return len(self.current_files)

    def filter_affects_rating(self) -> bool:
        return not all(self._current_star_filter)

    def has_active_tag_filter(self) -> bool:
        return bool(self._current_tag_filter)

    # -- RAW+JPG group mode ------------------------------------------------

    @property
    def group_mode(self) -> bool:
        return self._group_mode

    @property
    def group_map(self) -> Dict[str, FileGroup]:
        return self._group_map

    def toggle_group_mode(self):
        self._group_mode = not self._group_mode
        if self._group_mode:
            self._rebuild_group_map()
        else:
            self._group_map.clear()
            self._grouped_raw_paths.clear()
            self._raw_to_primary.clear()
        self.reapply_filters()
        state = "ON" if self._group_mode else "OFF"
        hidden = len(self._grouped_raw_paths) if self._group_mode else 0
        msg = f"RAW+JPG grouping: {state}"
        if hidden:
            msg += f" ({hidden} RAW files grouped)"
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE, source="thumbnail_view",
            timestamp=time.time(), message=msg, timeout=3000,
        ))

    def _rebuild_group_map(self):
        self._group_map, self._grouped_raw_paths = build_group_map(self.all_files)
        self._raw_to_primary = {}
        for primary, group in self._group_map.items():
            for raw in group.raw_files:
                self._raw_to_primary[raw] = primary

    def get_group_for_path(self, path: str) -> Optional[FileGroup]:
        group = self._group_map.get(path)
        if group:
            return group
        primary = self._raw_to_primary.get(path)
        if primary:
            return self._group_map.get(primary)
        return None

