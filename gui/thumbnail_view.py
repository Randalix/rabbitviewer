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
from PySide6.QtGui import QPixmap, QImage, QColor, QMouseEvent, QTransform
from gui.color_profile import apply_profile_pixmap
from PySide6.QtWidgets import (
    QVBoxLayout, QScrollArea, QWidget, QFrame
)

from gui.picture_base import PictureBase
from gui.components.virtual_grid_manager import VirtualGridManager
from gui.components.thumbnail_label import ThumbnailLabel
from core.selection import ReplaceSelectionCommand
from gui.selection_interaction import SelectionInteraction
from core.event_system import (event_system, EventType, EventData,
    StatusMessageEventData, ThumbnailHoveredEventData)
from network.daemon_signals import DaemonSignals
from core.event_system import ThumbnailOverlayEventData
from gui.overlay_manager import OverlayManager, OverlayDescriptor, BULK_THRESHOLD
from gui.overlay_renderers import render_stars, render_badge
from core.file_grouping import FileGroup
from gui.thumbnail_model import ThumbnailModel
from gui.viewport_prioritizer import ViewportPrioritizer
from gui.filter_controller import FilterController
from gui.thumbnail_notifications import NotificationHandler
from gui.components.edge_bar import EdgeBar, SelectionEdgeIndicator
from shiboken6 import isValid

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
    folderNavigated = Signal(str)            # emitted when user navigates into a folder

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.config_manager = config_manager
        self.gui_config = config_manager.get(
            "gui", {}) if config_manager else {}
        self.display_size = int(config_manager.get("thumbnail_size", 128))
        self.cache_dir = os.path.expanduser(config_manager.get("cache_dir"))
        self.spacing = self.gui_config.get("spacing", 5)
        self.service = None
        self.model = ThumbnailModel()

        # VirtualGridManager is created in _setupUI
        self._virtual_grid: Optional[VirtualGridManager] = None

        self.labels: Dict[int, ThumbnailLabel] = {}  # original_idx → materialized label (viewport only)
        self.pending_thumbnails = set()
        self._ratings_mode: bool = False
        self._rating_cache: Dict[str, int] = {}  # path → star count, populated when ratings mode is on
        self.middle_mouse_pressed = False
        self.middle_mouse_press_pos = None
        self._benchmark_timer = QElapsedTimer()
        self._last_load_time = 0
        self._last_redraw_time = 0

        # Selection interaction state machine
        self.selection = SelectionInteraction(
            self.model, self._update_label_selection,
            on_range_end=lambda: self.setCursor(Qt.ArrowCursor),
        )

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

        self.prioritizer = ViewportPrioritizer(self.model, self._send_heatmap)

        self._startup_thumbnails_emitted: bool = False
        self._startup_inline_thumb_count: int = 0

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

        # Delegated controllers
        self.filter_controller = FilterController(
            self, self.model, self._viewport_executor,
            is_loading=lambda: self._is_loading,
            label_count=lambda: len(self.labels),
            on_layout_rebuilt=self._rebuild_layout_for_filter,
        )
        self.filter_controller.filters_applied.connect(self.filtersApplied)
        self._notifications = NotificationHandler(self, self.model, self.prioritizer, self.filter_controller)
        self._notifications.preview_tick_timer.timeout.connect(self._tick_preview_loading)

        event_system.subscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)

        self.overlay_manager = OverlayManager(request_update=self._request_label_update)
        self.overlay_manager.register_renderer("stars", render_stars)
        self.overlay_manager.register_renderer("badge", render_badge)
        self._initial_files_signal.connect(self._on_initial_files_received)
        self._initial_thumbs_signal.connect(self._on_initial_thumbs_received)
        self._initial_folders_signal.connect(self._on_initial_folders_received)

        self._hovered_label: Optional[ThumbnailLabel] = None
        self._thumbnail_generated_signal.connect(self._on_thumbnail_ready, Qt.QueuedConnection)

        self._is_loading = False
        self._needs_heatmap_seed = False
        # why: separate from _is_loading — cached folders clear _is_loading in
        # _on_initial_files_received (immediate), uncached folders wait for scan_complete.
        self._folder_is_cached = False
        # Set by ScriptAPI.set_image_order() to prevent scan_complete from
        # overriding the script-applied sort with a default alphabetical resort.
        self._custom_sort_active = False


    def _initializeLayout(self):
        if self._virtual_grid:
            self._virtual_grid.update_layout()

    def _send_heatmap(self, upgrades, downgrades, fullres, cancels) -> None:
        """Callback for ViewportPrioritizer — dispatches to executor."""
        self._viewport_executor.submit(
            self.service.update_viewport_heatmap,
            upgrades, downgrades, fullres, cancels,
        )

    def _update_label_selection(self, orig_idx: int, selected: bool) -> None:
        """Callback for SelectionInteraction to update label highlight state."""
        label = self.labels.get(orig_idx)
        if label and isValid(label) and label.isVisible():
            label.setSelected(selected)

    def set_service(self, service):
        self.service = service
        self.filter_controller.service = service

    # -- External API compatibility shims ------------------------------------
    # These delegate to self.model so that main_window.py and script_api.py
    # can continue to access thumbnail_view.all_files etc. unchanged.

    @property
    def all_files(self):
        return self.model.all_files

    @property
    def current_files(self):
        return self.model.current_files

    @property
    def current_directory_path(self):
        return self.model.current_directory_path

    @current_directory_path.setter
    def current_directory_path(self, value):
        self.model.current_directory_path = value

    @property
    def _all_files_set(self):
        return self.model.all_files_set

    @property
    def _path_to_idx(self):
        return self.model.path_to_idx

    @property
    def image_states(self):
        return self.model.image_states

    @property
    def group_mode(self):
        return self.model.group_mode

    @property
    def group_map(self):
        return self.model.group_map

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
        return self.model.folder_nodes.keys()

    def get_hovered_image_path(self) -> Optional[str]:
        if self._hovered_label:
            return self._hovered_label.original_path
        return None

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.middle_mouse_pressed:
            self.scroll_area.setFocus()

        if self.middle_mouse_pressed:
            delta = event.pos() - self.middle_mouse_press_pos
            h_bar = self.scroll_area.horizontalScrollBar()
            v_bar = self.scroll_area.verticalScrollBar()

            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())

            self.middle_mouse_press_pos = event.pos()

        if self.selection.is_range_selection_active or (self.selection.is_in_drag and event.buttons() & Qt.LeftButton):
            current_idx = self._get_thumbnail_at_pos(event.pos())
            self.selection.on_mouse_move(current_idx)

    def _recycle_label(self, label: ThumbnailLabel):
        if not isValid(label):
            return
        # why: recycle clears hover before hide so thumbnailLeft fires and
        # _hovered_label never points to a hidden/reused widget.
        self._clear_hovered_label(label)
        if len(self._widget_pool) < self._pool_size:
            label.hide()
            label.setPixmap(QPixmap())
            label.loaded = False
            label.setSelected(False)
            label._display_rating = None
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
            if not isValid(label):
                # why: C++ object was deleted externally (deleteLater fired);
                # evict the stale wrapper so it can't propagate into _mat_labels.
                del self.labels[original_idx]
            else:
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
        if self._ratings_mode:
            label._display_rating = self._rating_cache.get(label.file_path)
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
            if event.type() == QEvent.Type.Resize:
                self._edge_bar_top.reposition()
                self._edge_bar_bottom.reposition()
                self._sel_indicator.update()
            elif event.type() == QEvent.Type.MouseButtonDblClick:
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
            start_index = self._get_thumbnail_at_pos(event.pos())
            self.selection.on_mouse_press(start_index, event.modifiers())

        super().mousePressEvent(event)

    def _recompute_selected_indices(self):
        self.selection.recompute_selected_indices()

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
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; }")

        self._grid_container = QWidget()
        self._grid_container.setContentsMargins(0, 0, 0, 0)
        self._grid_container.setStyleSheet("background: transparent;")

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

        bar_height = self.gui_config.get("edge_bar_height", 3)
        bar_color = QColor(self.gui_config.get("select_border_color", "orange"))
        self._edge_bar_top = EdgeBar(self.scroll_area.viewport(), edge="top", height=bar_height)
        self._edge_bar_bottom = EdgeBar(self.scroll_area.viewport(), edge="bottom", height=bar_height)
        self._edge_bar_top.add_indicator("selection", bar_color, lambda: self._sel_indicator.scroll_to_nearest("up"))
        self._edge_bar_bottom.add_indicator("selection", bar_color, lambda: self._sel_indicator.scroll_to_nearest("down"))
        self._sel_indicator = SelectionEdgeIndicator(
            self._edge_bar_top, self._edge_bar_bottom,
            self.model, self._virtual_grid, self.scroll_area, self.selection,
        )
        event_system.subscribe(EventType.SELECTION_CHANGED, self._on_selection_changed_indicators)

    def _setupResizeTimer(self):
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

    def load_directory(self, directory_path: str, recursive: bool = False):
        self._notifications.reset_startup(time.perf_counter())
        self._startup_thumbnails_emitted = False
        self._startup_inline_thumb_count = 0
        self._custom_sort_active = False
        logger.info("[startup] load_directory called for %s", directory_path)
        self.clear_layout()
        # Set scan state AFTER clear_layout() which resets _scan_active to False
        self._notifications.reset()
        self.model.scan_active = True
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="thumbnail_view",
            timestamp=time.time(),
            message=f"Scanning {os.path.basename(directory_path)}...",
            timeout=0 # a timeout of 0 makes it persistent until the next message
        ))
        self._is_loading = True
        self._folder_is_cached = False
        self.model.current_directory_path = directory_path
        self.model.is_recursive = recursive
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

            # Emit files and thumbnails first so the grid populates immediately
            self._initial_files_signal.emit(sorted(files))
            if thumbnail_paths:
                logger.info("[startup] %d cached thumbnail paths from initial response", len(thumbnail_paths))
                self._initial_thumbs_signal.emit(thumbnail_paths)

            # Discover subdirectories after files are emitted (slower DB queries)
            if not recursive:
                folder_nodes = self.service.get_subdirectories(directory_path)
                if folder_nodes:
                    logger.info("[folders] found %d subdirectories in %s", len(folder_nodes), directory_path)
                    self._initial_folders_signal.emit(folder_nodes)
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
        if self.model.all_files:
            self.filter_controller.reapply_filters()

    @Slot(list)
    def _on_initial_folders_received(self, folder_nodes: list):
        """Insert folder entries at the start of the grid (before images)."""
        if not folder_nodes:
            return
        folder_paths = []
        for node in folder_nodes:
            self.model.folder_nodes[node.path] = node
            folder_paths.append(node.path)
        # Prepend folders so they appear at the top of the grid.
        # _add_image_batch deduplicates via _all_files_set.
        self._add_image_batch(folder_paths)
        logger.info("[folders] inserted %d folder cards into grid", len(folder_paths))

    @Slot(dict)
    def _on_initial_thumbs_received(self, thumb_map: dict):
        for source_path, thumb_path in thumb_map.items():
            orig_idx = self.model.path_to_idx.get(source_path, -1)
            if orig_idx < 0:
                # Files not yet in all_files — store for later
                self.model.initial_thumb_paths[source_path] = thumb_path
                continue
            self.model.thumb_path_cache[orig_idx] = thumb_path

            # If this label is already materialized (placeholder), load the
            # pixmap now.  The files signal is queued before thumbs, so labels
            # are typically created as placeholders before thumb paths arrive.
            label = self.labels.get(orig_idx)
            if label and orig_idx not in self.model.pixmap_cache:
                image = QImage(thumb_path)
                if not image.isNull():
                    image = self._apply_db_orientation(image, source_path)
                    pixmap = apply_profile_pixmap(image)
                    self.model.pixmap_cache[orig_idx] = pixmap
                    label.updateThumbnail(pixmap)
                    label.loaded = True
                    state = self.model.image_states.get(orig_idx)
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
                idx = self.model.path_to_idx.get(path)
                if idx is not None:
                    self.overlay_manager.show(idx, descriptor)
                    label = self.labels.get(idx)
                    if label:
                        label.update()
                        matched += 1
            logger.debug("[overlay] show: %d/%d paths matched to labels", matched, len(paths))

            # Keep rating cache and persistent display in sync when a rating overlay fires.
            if self._ratings_mode and event_data.renderer_name == "stars":
                count = event_data.params.get("count", 0)
                for path in paths:
                    self._rating_cache[path] = count
                    idx = self.model.path_to_idx.get(path)
                    if idx is not None:
                        label = self.labels.get(idx)
                        if label:
                            label._display_rating = count

        elif action == "remove":
            for path in paths:
                idx = self.model.path_to_idx.get(path)
                if idx is not None:
                    self.overlay_manager.remove(idx, event_data.overlay_id)
                    label = self.labels.get(idx)
                    if label:
                        label.update()

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._notifications.set_daemon_signals(daemon_signals)

    # -- Ratings display mode -----------------------------------------------

    def toggle_ratings_mode(self) -> None:
        self._ratings_mode = not self._ratings_mode
        if self._ratings_mode:
            self._fetch_and_apply_ratings()
        else:
            self._rating_cache.clear()
            for label in self.labels.values():
                label._display_rating = None
                label.update()

    def _fetch_and_apply_ratings(self) -> None:
        if not self.service:
            return
        # Phase 1: fetch only visible labels immediately so stars appear without delay.
        visible_paths = [label.file_path for label in self.labels.values()]
        all_paths = list(self.model.all_files)

        def _fetch():
            try:
                # Visible first — small batch, fast round-trip.
                if visible_paths:
                    result = self.service.get_metadata_batch(visible_paths)
                    if result:
                        for path, meta in result.items():
                            self._rating_cache[path] = int(meta.get("rating", 0) or 0)
                    QTimer.singleShot(0, self._apply_cached_ratings_to_labels)
                # Phase 2: remaining paths (background, populates cache for scroll-in).
                remaining = [p for p in all_paths if p not in self._rating_cache]
                if remaining:
                    result = self.service.get_metadata_batch(remaining)
                    if result:
                        for path, meta in result.items():
                            self._rating_cache[path] = int(meta.get("rating", 0) or 0)
            except Exception:
                logger.exception("[ratings] failed to fetch ratings for ratings mode")

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_cached_ratings_to_labels(self) -> None:
        if not self._ratings_mode:
            return
        for original_idx, label in self.labels.items():
            rating = self._rating_cache.get(label.file_path)
            if rating is not None:
                label._display_rating = rating
                label.update()

    def path_belongs_to_current_directory(self, path: str) -> bool:
        """Return True if *path* is (or is under) the currently browsed directory."""
        cur = self.current_directory_path
        if not cur:
            return False
        rp = os.path.realpath(path)
        rc = os.path.realpath(cur)
        return rp == rc or rp.startswith(rc + os.sep)

    def _add_image_batch(self, files: List[str]):
        """Adds a batch of new file paths.

        Delegates data bookkeeping to self.model.add_files(), then coordinates
        widget creation and layout updates.
        """
        if not files:
            return

        result = self.model.add_files(files)
        if result is None:
            logger.debug("[virtual] _add_image_batch: all %d files already known, skipping", len(files))
            return

        logger.info(
            "[virtual] _add_image_batch: +%d new files (all_files=%d)",
            len(result.new_files), len(self.model.all_files),
        )
        if not self.model.hidden_indices and not self.model.group_mode:
            if self.model.scan_active:
                self.model.append_to_visible(result.new_files)
                self._notifications.scan_batch_pending = True
                logger.debug(
                    "[virtual] _scan_active append: +%d files (current_files=%d, coalesce_active=%s)",
                    len(result.new_files), len(self.model.current_files), self._notifications._scan_coalesce_timer.isActive(),
                )
                if not self._notifications.scan_first_batch_flushed:
                    self._notifications._scan_coalesce_timer.stop()
                    self._notifications._flush_scan_layout()
                elif not self._notifications._scan_coalesce_timer.isActive():
                    self._notifications._scan_coalesce_timer.start()
            else:
                if len(result.new_files) > 50:
                    self.reorder_files(sorted(self.model.all_files))
                else:
                    self._insert_sorted_incremental(result.new_files)
        else:
            self.filter_controller.reapply_filters()

    def _insert_sorted_incremental(self, new_files: list):
        """Insert new files at sorted positions without full rebuild."""
        self.model.insert_sorted(new_files)

        if self._virtual_grid:
            mapping = self.model.original_to_visible
            self._virtual_grid.reindex_labels(
                len(self.model.current_files),
                lambda label: mapping.get(label._original_idx),
                self._recycle_virtual_label,
            )
            self._sync_virtual_viewport()

        self.model.last_layout_file_count = len(self.model.all_files)
        QTimer.singleShot(100, self._prioritize_visible_thumbnails)

    def add_images(self, image_paths: List[str]) -> None:
        normalized = [os.path.abspath(p) for p in image_paths]
        self._add_image_batch(normalized)

    def remove_images(self, paths: List[str]):
        if not paths:
            return

        self._benchmark_timer.start()

        try:
            result = self.model.remove_files(paths)

            # -- Surgical grid splice — only recycle deleted labels -------
            if self._virtual_grid and result.removed_vis_indices:
                vis_to_orig = self.model.visible_to_original

                def _update_label(label, new_vis_idx):
                    new_orig = vis_to_orig[new_vis_idx]
                    label._original_idx = new_orig
                    label.file_path = self.model.all_files[new_orig]

                self._virtual_grid.splice_items(
                    result.removed_vis_indices,
                    len(self.model.current_files),
                    self._recycle_label,
                    _update_label,
                )

                # Re-key self.labels from old original_idx → new original_idx.
                new_labels = {}
                for label in self._virtual_grid.materialized_labels():
                    new_labels[label._original_idx] = label
                self.labels = new_labels
            elif self._virtual_grid:
                # Nothing visible was removed (all removed items were hidden).
                self._virtual_grid.set_total_items(len(self.model.current_files))
                # Re-key self.labels — hidden-file removal can shift orig_idx of
                # surviving visible files, leaving labels under stale keys.  Without
                # this, _label_updater calls for the new orig_idx find None and skip
                # the visual update, leaving selected=True highlights on deselected
                # labels (the "zombie selection" bug).
                new_labels = {}
                for label in self._virtual_grid.materialized_labels():
                    new_orig = self.model.path_to_idx.get(label.file_path)
                    if new_orig is not None:
                        label._original_idx = new_orig
                        new_labels[new_orig] = label
                self.labels = new_labels

            # -- Clear hover if the hovered label was deleted ----------------
            if self._hovered_label is not None:
                if self._hovered_label.file_path in result.removed_paths:
                    self._hovered_label = None
                    self.thumbnailLeft.emit()

            # -- Preserve selection of surviving files -----------------------
            # Recompute _selected_indices to new orig_idx values BEFORE publishing
            # SELECTION_CHANGED so on_selection_changed sees a correct delta.
            surviving_selection = self.selection.current_selection - result.removed_paths
            self._recompute_selected_indices()
            if surviving_selection != self.selection.current_selection:
                cmd = ReplaceSelectionCommand(paths=surviving_selection, source="thumbnail_view", timestamp=time.time())
                event_system.publish(cmd)
            self._sync_virtual_viewport()
            # why: _recompute_selected_indices() runs before ReplaceSelectionCommand
            # so on_selection_changed always sees an empty delta and never fires
            # _update_label_selection.  Reconcile directly against committed state.
            self._reconcile_selection_visuals()
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)

            self._last_redraw_time = self._benchmark_timer.elapsed() / 1000.0
            self.benchmarkComplete.emit("Redraw", self._last_redraw_time)

        except (KeyError, IndexError) as e:
            # why: index/path maps can desync if a watchdog removal races with an
            # in-progress remove_images call on the same set of paths.
            logger.error("Error removing images: %s", e, exc_info=True)

    def reorder_files(self, ordered_paths: list):
        """Reorder all_files to match *ordered_paths* and refresh the layout."""
        if not self.model.reorder(ordered_paths):
            return

        # Recycle all materialized labels — re-materialized with new indices
        if self._virtual_grid:
            self._virtual_grid.clear(self._recycle_label)
        self.labels.clear()
        self._recompute_selected_indices()

        self._rebuild_layout_for_filter()

    def scroll_to_top(self, image_path: str) -> None:
        """Scroll so the row containing *image_path* is the first visible row,
        then materialize widgets.
        """
        original_idx = self.model.path_to_idx.get(image_path, -1)
        if original_idx < 0:
            return
        visible_idx = self.model.original_to_visible.get(original_idx)
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
        affected = self.model.invalidate(image_paths)
        for path in image_paths:
            self.prioritizer.invalidate_thumb_pairs(path)
        if clear_labels:
            for idx in affected:
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
            idx = self.model.path_to_idx.get(path)
            if idx is None:
                continue
            pixmap = self.model.pixmap_cache.get(idx)
            if pixmap and not pixmap.isNull():
                rotated = pixmap.transformed(transform, Qt.SmoothTransformation)
                scaled = rotated.scaled(self.display_size, self.display_size,
                                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.model.pixmap_cache[idx] = scaled
                label = self.labels.get(idx)
                if label:
                    label.updateThumbnail(scaled)
            # Clear so _tick_preview_loading accepts the regenerated thumbnail.
            self.model.thumb_path_cache.pop(idx, None)
            self.prioritizer.invalidate_thumb_pairs(path)

    def ensure_visible(self, original_idx: int, center: bool = False):
        """Scroll so the thumbnail at *original_idx* is in the viewport, then
        materialize widgets so it actually appears.
        """
        visible_idx = self.model.original_to_visible.get(original_idx)
        if visible_idx is None:
            logger.debug("Original index %d not visible (filtered out)", original_idx)
            return

        if self._virtual_grid:
            self._virtual_grid.ensure_visible(visible_idx, center)
            self._sync_virtual_viewport()

    def closeEvent(self, event):
        self.selection.dispose()
        event_system.unsubscribe(EventType.SELECTION_CHANGED, self._on_selection_changed_indicators)
        event_system.unsubscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
        self.filter_controller.dispose()
        self._notifications.dispose()

        # Stop timers
        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_priority_update_timer'):
            self._priority_update_timer.stop()
        if hasattr(self, '_scroll_idle_timer'):
            self._scroll_idle_timer.stop()
        if hasattr(self, '_viewport_executor'):
            # wait=False: in-flight viewport calls are best-effort priority hints; the
            # socket client handles broken-pipe errors on its own after widget teardown.
            self._viewport_executor.shutdown(wait=False)

        # Clear pixmap/path caches
        self.model.pixmap_cache.clear()
        self.model.thumb_path_cache.clear()

        # Clear widget pool
        for label in self._widget_pool:
            label.deleteLater()
        self._widget_pool.clear()

        super().closeEvent(event)

    def clear_layout(self):
        # Stop timers and cleanup thread
        if hasattr(self, '_resize_timer'):
            self._resize_timer.stop()
        if hasattr(self, '_priority_update_timer'):
            self._priority_update_timer.stop()
        if hasattr(self, '_scroll_idle_timer'):
            self._scroll_idle_timer.stop()
        self.filter_controller.reset()
        self._notifications.reset()
        self.model.scan_active = False
        # Recycle all materialized labels via VirtualGridManager
        if hasattr(self, '_virtual_grid') and self._virtual_grid:
            self._virtual_grid.clear(self._recycle_label)

        self.labels.clear()
        cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
        event_system.publish(cmd)
        self.selection.reset()

        self.pending_thumbnails.clear()
        self.model.clear()
        # Cancel any in-flight speculative fullres tasks from the old directory
        cancel_paths = self.prioritizer.clear()
        if cancel_paths and self.service:
            self._viewport_executor.submit(
                self.service.update_viewport_heatmap,
                [], [], [], cancel_paths,
            )
        self._hovered_label = None

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

        original_idx = self.model.path_to_idx.get(original_path, -1)
        if original_idx >= 0:
            # Always cache the pixmap (even errors, so we don't re-request)
            self.model.pixmap_cache[original_idx] = pixmap
            state = self.model.image_states.get(original_idx)
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

    def _tick_preview_loading(self):
        """Drains the preview queue via the prioritizer and loads QImages."""
        batch = self.prioritizer.drain_preview_batch()

        for image_path, thumbnail_path in batch:
            orig_idx = self.model.path_to_idx.get(image_path, -1)
            if orig_idx < 0:
                continue
            state = self.model.image_states.get(orig_idx)
            if state and state.loaded:
                if self.model.thumb_path_cache.get(orig_idx) == thumbnail_path:
                    continue
            image = QImage(thumbnail_path)
            if not image.isNull():
                image = self._apply_db_orientation(image, image_path)
                self.model.thumb_path_cache[orig_idx] = thumbnail_path
                self._thumbnail_generated_signal.emit(image_path, image, None)
            else:
                logger.warning("Failed to load thumbnail: %s", thumbnail_path)

        if not self.prioritizer.has_pending_previews:
            self._notifications.preview_tick_timer.stop()
            if self._notifications.startup_t0 is not None and not self._startup_thumbnails_emitted:
                self._startup_thumbnails_emitted = True
                elapsed_ms = (time.perf_counter() - self._notifications.startup_t0) * 1000
                logger.info("[startup] initial thumbnails drawn: %.0f ms after load_directory", elapsed_ms)

    def get_benchmark_results(self) -> dict:
        return {
            "Initial Load Time": self._last_load_time,
            "Redraw Time": self._last_redraw_time,
            "Total Images": len(self.model.current_files),
            "Cached Images": len(self.model.pixmap_cache),
            "Pending Images": len(self.pending_thumbnails)
        }

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.selection.is_in_drag:
            entered_range = self.selection.on_mouse_release()
            if entered_range:
                self.setCursor(Qt.CrossCursor)

        super().mouseReleaseEvent(event)

    def _restore_pre_click_selection(self):
        """Restore selection to the state before the last mouse press (used by double-click)."""
        self.selection.restore_pre_click_selection()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
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
        if path in self.model.folder_nodes:
            self.folderNavigated.emit(path)
        else:
            self.doubleClicked.emit(path)

    def navigate_to_folder(self, folder_path: str):
        """Navigate into a subdirectory, pushing current path onto the stack."""
        if self.current_directory_path:
            self.model.navigation_stack.append(self.current_directory_path)
        self.load_directory(folder_path, recursive=False)

    def navigate_to_parent(self):
        """Navigate back to the previous directory from the stack."""
        if self.model.navigation_stack:
            parent = self.model.navigation_stack.pop()
            self.load_directory(parent, recursive=False)

    def navigate_to_file(self, file_path: str):
        if file_path in self.model.current_files:
            self.setHighlightedThumbnail(file_path)

    def setHighlightedThumbnail(self, image_path: str):
        """Briefly highlight a thumbnail on return from picture view without changing selection."""
        try:
            original_idx = self.model.path_to_idx.get(image_path, -1)
            if original_idx < 0:
                logger.warning("Image %s not found in all_files during highlight attempt.", image_path)
                return

            if original_idx in self.model.original_to_visible:
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
            return self.model.visible_to_original.get(visible_idx)

        return None

    def _rebuild_layout_for_filter(self):
        """Callback for FilterController: widget operations after filter layout rebuild."""
        if not self._virtual_grid:
            return

        # Recycle all materialized labels then re-materialize for the new mapping
        # why: hover is cleared inside _recycle_label so thumbnailLeft fires
        # correctly regardless of whether the hovered item was filtered or not.
        self._virtual_grid.clear(self._recycle_label)
        self.labels.clear()
        self._virtual_grid.set_total_items(len(self.model.current_files))
        self._virtual_grid.update_layout()
        self._sync_virtual_viewport()

        if self._needs_heatmap_seed:
            self._needs_heatmap_seed = False
            self._prioritize_visible_thumbnails()
        else:
            QTimer.singleShot(100, self._prioritize_visible_thumbnails)

    def _sync_virtual_viewport(self):
        if self._virtual_grid and self.model.current_files:
            self._virtual_grid.sync_viewport(
                self._materialize_label,
                self._recycle_virtual_label,
            )

    def _get_first_visible_file(self) -> Optional[str]:
        """Return the file path at the top-left of the visible viewport."""
        if not self._virtual_grid or not self.model.current_files:
            return None
        first_row, _ = self._virtual_grid.get_visible_rows()
        first_vis_idx = first_row * self._virtual_grid.columns
        if 0 <= first_vis_idx < len(self.model.current_files):
            return self.model.current_files[first_vis_idx]
        return None

    def _materialize_label(self, visible_idx: int) -> ThumbnailLabel:
        original_idx = self.model.visible_to_original[visible_idx]
        file_path = self.model.all_files[original_idx]

        label = self._get_or_create_label(file_path, original_idx)
        label._original_idx = original_idx
        label._overlay_manager = self.overlay_manager

        # Configure folder cards
        if file_path in self.model.folder_nodes:
            was_folder = getattr(label, 'is_folder', False)
            label.is_folder = True
            label._folder_node = self.model.folder_nodes.get(file_path)
            label._cache_folder_previews()
            label.loaded = True
            state = self.model.image_states.get(original_idx)
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
            pixmap = self.model.pixmap_cache.get(original_idx)
            if pixmap is None:
                thumb_path = self.model.thumb_path_cache.get(original_idx)
                if thumb_path:
                    image = QImage(thumb_path)
                    if not image.isNull():
                        image = self._apply_db_orientation(image, file_path)
                        pixmap = apply_profile_pixmap(image)
                        self.model.pixmap_cache[original_idx] = pixmap
            if pixmap:
                label.updateThumbnail(pixmap)
                label.loaded = True
                state = self.model.image_states.get(original_idx)
                if state:
                    state.loaded = True

        # Apply selection state — during an active drag, use the preview set
        # so recycled labels re-appear with the correct highlight.
        label.setSelected(self.selection.is_selected(original_idx))

        self.labels[original_idx] = label
        return label

    def _reconcile_selection_visuals(self) -> None:
        """Reset every materialized label's selection border to match committed state.

        why: after remove_images, _recompute_selected_indices() runs before
        ReplaceSelectionCommand is published, so on_selection_changed always
        sees an empty delta and never calls _update_label_selection.  Any label
        that still has selected=True from a drag preview is never cleared.
        Iterating the (small) materialized set and comparing against the
        authoritative _selected_indices is the only reliable fix.
        """
        selected = self.selection.selected_indices
        for orig_idx, label in list(self.labels.items()):
            if isValid(label):
                label.setSelected(orig_idx in selected)

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
        self._sel_indicator.update()
        if not self._priority_update_timer.isActive():
            self._prioritize_visible_thumbnails()  # immediate first update
            self._priority_update_timer.start()
        self._scroll_idle_timer.start()  # reset idle countdown

    def _on_selection_changed_indicators(self, event_data) -> None:
        self._sel_indicator.on_selection_changed()

    def _on_scroll_idle(self):
        """Called when no scroll events have fired for 200ms — stop the
        repeating heatmap timer and fire one final update."""
        self._priority_update_timer.stop()
        self._prioritize_visible_thumbnails()

    def _prioritize_visible_thumbnails(self):
        """Delegates heatmap computation to the ViewportPrioritizer."""
        if not self.service or not self.labels or not self.model.current_files or not self._virtual_grid:
            return

        columns = self._virtual_grid.columns
        if columns <= 0:
            return

        hovered_orig_idx = None
        if self._hovered_label:
            hovered_orig_idx = self._label_to_original_idx(self._hovered_label)

        self.prioritizer.compute_and_send(
            hovered_orig_idx,
            self._virtual_grid.get_visible_rows(),
            columns,
            len(self.model.current_files),
            self._is_loading,
        )

    def get_visible_count(self) -> int:
        return len(self.model.current_files)

    def filter_affects_rating(self) -> bool:
        return self.model.filter_affects_rating()

    def has_active_tag_filter(self) -> bool:
        return self.model.has_active_tag_filter()

    # -- RAW+JPG group mode ------------------------------------------------

    def toggle_group_mode(self):
        msg = self.model.toggle_group_mode()
        self.filter_controller.reapply_filters()
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE, source="thumbnail_view",
            timestamp=time.time(), message=msg, timeout=3000,
        ))

    def get_group_for_path(self, path: str) -> Optional[FileGroup]:
        return self.model.get_group_for_path(path)

