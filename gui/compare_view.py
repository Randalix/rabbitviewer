import math
import logging
logger = logging.getLogger(__name__)
import threading
import time

from PySide6.QtWidgets import QWidget, QGridLayout
from PySide6.QtCore import Qt, Signal, QPointF, QSizeF, QPoint, QRect
from PySide6.QtGui import QColor, QPainter, QPen, QImage, QMouseEvent, QPaintEvent, QResizeEvent, QWheelEvent, QKeyEvent, QTransform

from core.event_system import EventType, StatusMessageEventData, StatusSection, ThumbnailOverlayEventData, event_system
from .overlay_manager import OverlayManager, OverlayDescriptor
from .overlay_renderers import render_stars, render_badge
from .picture_base import PictureBase, ViewState

_DIVIDER_COLOR = QColor(200, 200, 200)
_DIVIDER_WIDTH = 2
_DIVIDER_GRAB_ZONE = 8  # pixels on each side of divider that count as grab area


class _CompareSplit(QWidget):

    _image_ready = Signal(QImage)  # marshals loaded image to GUI thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self.update)
        self._current_path = None
        self._service = None
        self._is_panning = False
        self._last_mouse_pos = QPoint()
        self._interacting = False
        self._user_navigating = False
        self._selected = False
        self._overlay_manager = OverlayManager(request_update=lambda _idx: self.update())
        self._overlay_manager.register_renderer("stars", render_stars)
        self._overlay_manager.register_renderer("badge", render_badge)
        self._image_ready.connect(self._on_image_ready)

        # Offset from master: client_pos = master_pos + center_offset
        self.center_offset = QPointF(0.0, 0.0)
        self.zoom_ratio = 1.0  # client_zoom = master_zoom * zoom_ratio

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def selected(self) -> bool:
        return self._selected

    @selected.setter
    def selected(self, value: bool):
        if self._selected != value:
            self._selected = value
            self.update()

    def fetch_image(self, image_path: str, service):
        if not service:
            return
        self._service = service
        result = service.request_view_image(image_path)
        if result is None:
            return
        # why: QImage construction + load is thread-safe (no GUI dependency);
        # _image_ready signal marshals to GUI thread before any widget mutation.
        image = QImage()
        if result.get('view_image_source') == "memory":
            image_bytes = service.get_cached_view_image(image_path)
            if image_bytes:
                image.loadFromData(image_bytes)
        elif result.get('view_image_path'):
            image.load(result['view_image_path'])
        if not image.isNull():
            self._current_path = image_path
            self._image_ready.emit(image)

    def _on_image_ready(self, image: QImage):
        self._picture_base.setImage(image)
        self._apply_orientation()
        self._picture_base.setFitMode(True)
        self.update()

    def _apply_orientation(self) -> None:
        if not self._service or not self._current_path:
            return
        try:
            resp = self._service.get_metadata_batch([self._current_path])
            orientation = resp.get(self._current_path, {}).get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            return
        self._picture_base.setOrientationFromExif(orientation)

    def apply_view_state(self, center: QPointF, zoom: float, fit_mode: bool):
        if fit_mode:
            self._picture_base.setFitMode(True)
        else:
            self._picture_base.setZoom(zoom, center)

    def view_state(self) -> ViewState:
        return self._picture_base.viewState()

    @property
    def interacting(self) -> bool:
        return self._interacting

    @interacting.setter
    def interacting(self, value: bool):
        self._interacting = value

    @property
    def user_navigating(self) -> bool:
        return self._user_navigating

    def zoom_in(self, factor: float = 1.25):
        self._user_navigating = True
        self._picture_base.zoomIn(factor)
        self._user_navigating = False

    def zoom_out(self, factor: float = 1.25):
        self._user_navigating = True
        self._picture_base.zoomOut(factor)
        self._user_navigating = False

    def cleanup(self):
        self._picture_base.cleanup_subscriptions()

    # -- Qt event overrides --------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._picture_base.has_image():
            return
        painter = QPainter(self)
        if not self._interacting:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setTransform(self._picture_base.calculateTransform())
        painter.drawImage(0, 0, self._picture_base.get_image())

        # Overlays (star rating, etc.)
        if self._overlay_manager.has_overlays(0):
            painter.resetTransform()
            overlay_size = min(self.width(), self.height()) // 3
            ox = (self.width() - overlay_size) // 2
            oy = (self.height() - overlay_size) // 2
            self._overlay_manager.paint(painter, QRect(ox, oy, overlay_size, overlay_size), 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            parent = self.parent()
            if isinstance(parent, CompareView):
                parent._on_split_clicked(self, event.modifiers())
            self._is_panning = True
            self._interacting = True
            self._user_navigating = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
            self._interacting = True
            self._user_navigating = True
            anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(anchor, QPointF(event.position()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_panning:
            delta = event.position().toPoint() - self._last_mouse_pos
            self._last_mouse_pos = event.position().toPoint()
            transform = self._picture_base.calculateTransform()
            inv_transform, invertible = transform.inverted()
            if invertible:
                delta_normalized = inv_transform.map(QPointF(delta)) - inv_transform.map(QPointF(0, 0))
                current_center = self._picture_base.viewState().center
                new_center = QPointF(
                    current_center.x() - delta_normalized.x() / self._picture_base.paddedRect().width(),
                    current_center.y() + delta_normalized.y() / self._picture_base.paddedRect().height(),
                )
                self._picture_base.setCenter(new_center)
        elif self._picture_base.isDragZooming():
            self._picture_base.updateDragZoom(QPointF(event.position()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self._end_interaction()
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
            self._end_interaction()

    def _end_interaction(self):
        self._interacting = False
        self._user_navigating = False
        self.update()
        parent = self.parent()
        if isinstance(parent, CompareView):
            parent._finish_interaction(self)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._user_navigating = True
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
                self._picture_base.setZoom(1.0, click_pos)
            else:
                self._picture_base.setFitMode(True)
            self._user_navigating = False

    def enterEvent(self, event) -> None:
        parent = self.parent()
        if isinstance(parent, CompareView):
            parent._on_split_hovered(self)
        if self._current_path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="compare_view",
                timestamp=time.time(),
                message=self._current_path,
                section=StatusSection.FILEPATH,
            ))

    def leaveEvent(self, event) -> None:
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="compare_view",
            timestamp=time.time(),
            message="",
            section=StatusSection.FILEPATH,
        ))

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._user_navigating = True
        center = self._picture_base.screenToNormalized(QPointF(event.position()))
        factor = 1.25
        if delta > 0:
            self._picture_base.zoomIn(factor, center)
        else:
            self._picture_base.zoomOut(factor, center)
        self._user_navigating = False


# ---------------------------------------------------------------------------
# Split-line before/after view (exactly 2 images, shared zoom/pan)
# ---------------------------------------------------------------------------

_LEFT = 0
_RIGHT = 1


class _SplitLineView(QWidget):
    """Before/after comparison with a draggable vertical divider.

    Both images share a single PictureBase so zoom and pan are locked together.
    The left image uses the PictureBase dimensions as the reference frame.
    """

    _image_ready = Signal(int, QImage)  # (side, image) marshals to GUI thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)

        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self.update)

        self._images: list[QImage | None] = [None, None]
        self._paths: list[str | None] = [None, None]
        self._service = None
        self._selected_side: int = _LEFT  # which side is selected for rating
        self._both_selected = False

        # Divider position as fraction of viewport width (0.0–1.0)
        self._divider_x = 0.5
        self._dragging_divider = False
        self._is_panning = False
        self._interacting = False
        self._last_mouse_pos = QPoint()

        # Per-side overlay managers (idx 0 = always, one manager per side)
        self._overlay_managers: list[OverlayManager] = []
        for _ in range(2):
            om = OverlayManager(request_update=lambda _idx: self.update())
            om.register_renderer("stars", render_stars)
            om.register_renderer("badge", render_badge)
            self._overlay_managers.append(om)

        self._image_ready.connect(self._on_image_ready)

    # -- public interface used by CompareView --------------------------------

    @property
    def selected_paths(self) -> list[str]:
        if self._both_selected:
            return [p for p in self._paths if p]
        path = self._paths[self._selected_side]
        return [path] if path else []

    def path_to_side(self, path: str) -> int | None:
        if self._paths[_LEFT] == path:
            return _LEFT
        if self._paths[_RIGHT] == path:
            return _RIGHT
        return None

    def load_images(self, left_path: str, right_path: str, service):
        self._service = service
        self._paths = [left_path, right_path]
        self._images = [None, None]
        self._selected_side = _LEFT
        self._both_selected = False
        self._divider_x = 0.5

        for side, path in enumerate(self._paths):
            threading.Thread(
                target=self._load_side_image, args=(side, path), daemon=True
            ).start()

    def show_overlay(self, side: int, descriptor: OverlayDescriptor) -> None:
        self._overlay_managers[side].show(0, descriptor)
        self.update()

    def remove_overlay(self, side: int, overlay_id: str) -> None:
        self._overlay_managers[side].remove(0, overlay_id)
        self.update()

    def zoom_in(self, factor: float = 1.25):
        self._picture_base.zoomIn(factor)

    def zoom_out(self, factor: float = 1.25):
        self._picture_base.zoomOut(factor)

    def cleanup(self):
        self._picture_base.cleanup_subscriptions()

    # -- image loading -------------------------------------------------------

    def _load_side_image(self, side: int, path: str) -> None:
        if not self._service:
            return
        try:
            result = self._service.request_view_image(path)
            if result is None:
                return
            image = QImage()
            if result.get('view_image_source') == "memory":
                image_bytes = self._service.get_cached_view_image(path)
                if image_bytes:
                    image.loadFromData(image_bytes)
            elif result.get('view_image_path'):
                image.load(result['view_image_path'])
            if not image.isNull():
                image = self._orient_image(image, path)
                self._image_ready.emit(side, image)
        except Exception as e:  # why: service/plugin errors must not crash the worker thread
            logger.error(f"_SplitLineView: failed to load {path}: {e}", exc_info=True)

    def _orient_image(self, image: QImage, path: str) -> QImage:
        if not self._service:
            return image
        try:
            resp = self._service.get_metadata_batch([path])
            orientation = resp.get(path, {}).get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            return image
        degrees = PictureBase.EXIF_ORIENTATION_DEGREES.get(orientation, 0)
        if degrees == 0:
            return image
        return image.transformed(QTransform().rotate(degrees), Qt.SmoothTransformation)

    def _on_image_ready(self, side: int, image: QImage) -> None:
        self._images[side] = image
        # Use left image to set PictureBase dimensions (reference frame)
        if side == _LEFT:
            self._picture_base.setImage(image)
            self._picture_base.setFitMode(True)
        self.update()

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        left_img = self._images[_LEFT]
        right_img = self._images[_RIGHT]
        if not left_img and not right_img:
            return

        w, h = self.width(), self.height()
        divider_px = int(self._divider_x * w)
        transform = self._picture_base.calculateTransform()

        painter = QPainter(self)
        if not self._interacting:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # Left half
        if left_img:
            painter.save()
            painter.setClipRect(0, 0, divider_px, h)
            painter.setTransform(transform)
            painter.drawImage(0, 0, left_img)
            painter.restore()

        # Right half
        if right_img:
            painter.save()
            painter.setClipRect(divider_px, 0, w - divider_px, h)
            painter.setTransform(transform)
            painter.drawImage(0, 0, right_img)
            painter.restore()

        # Divider line
        painter.setPen(QPen(_DIVIDER_COLOR, _DIVIDER_WIDTH))
        painter.drawLine(divider_px, 0, divider_px, h)

        # Overlays per side
        for side, (x_start, side_w) in enumerate([(0, divider_px), (divider_px, w - divider_px)]):
            if self._overlay_managers[side].has_overlays(0):
                overlay_size = min(side_w, h) // 3
                ox = x_start + (side_w - overlay_size) // 2
                oy = (h - overlay_size) // 2
                self._overlay_managers[side].paint(painter, QRect(ox, oy, overlay_size, overlay_size), 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    # -- mouse handling ------------------------------------------------------

    def _divider_px(self) -> int:
        return int(self._divider_x * self.width())

    def _in_grab_zone(self, x: int) -> bool:
        return abs(x - self._divider_px()) <= _DIVIDER_GRAB_ZONE

    def _side_at(self, x: int) -> int:
        return _LEFT if x < self._divider_px() else _RIGHT

    def mousePressEvent(self, event: QMouseEvent) -> None:
        x = int(event.position().x())
        if event.button() == Qt.LeftButton:
            if self._in_grab_zone(x):
                self._dragging_divider = True
                self.setCursor(Qt.SplitHCursor)
                return

            # Selection
            side = self._side_at(x)
            if event.modifiers() & Qt.ControlModifier:
                if self._both_selected:
                    # Deselect the clicked side, keep the other
                    self._both_selected = False
                    self._selected_side = _RIGHT if side == _LEFT else _LEFT
                elif self._selected_side == side:
                    pass  # already selected, nothing to toggle
                else:
                    self._both_selected = True
            else:
                self._both_selected = False
                self._selected_side = side

            # Pan
            self._is_panning = True
            self._interacting = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            self.update()

        elif event.button() == Qt.RightButton:
            self._interacting = True
            anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(anchor, QPointF(event.position()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x = int(event.position().x())

        if self._dragging_divider:
            self._divider_x = max(0.05, min(0.95, x / max(self.width(), 1)))
            self.update()
            return

        if self._is_panning:
            delta = event.position().toPoint() - self._last_mouse_pos
            self._last_mouse_pos = event.position().toPoint()
            transform = self._picture_base.calculateTransform()
            inv_transform, invertible = transform.inverted()
            if invertible:
                delta_normalized = inv_transform.map(QPointF(delta)) - inv_transform.map(QPointF(0, 0))
                current_center = self._picture_base.viewState().center
                new_center = QPointF(
                    current_center.x() - delta_normalized.x() / self._picture_base.paddedRect().width(),
                    current_center.y() + delta_normalized.y() / self._picture_base.paddedRect().height(),
                )
                self._picture_base.setCenter(new_center)
        elif self._picture_base.isDragZooming():
            self._picture_base.updateDragZoom(QPointF(event.position()))
        else:
            # Update cursor for grab zone hover
            if self._in_grab_zone(x):
                self.setCursor(Qt.SplitHCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

        # Publish hovered image path based on side
        self._publish_hovered_path(x)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self._dragging_divider:
                self._dragging_divider = False
                self.setCursor(Qt.ArrowCursor)
                return
            self._is_panning = False
            self._interacting = False
            self.setCursor(Qt.ArrowCursor)
            self.update()
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
            self._interacting = False
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and not self._in_grab_zone(int(event.position().x())):
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
                self._picture_base.setZoom(1.0, click_pos)
            else:
                self._picture_base.setFitMode(True)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        center = self._picture_base.screenToNormalized(QPointF(event.position()))
        factor = 1.25
        if delta > 0:
            self._picture_base.zoomIn(factor, center)
        else:
            self._picture_base.zoomOut(factor, center)

    def _publish_hovered_path(self, x: int) -> None:
        side = self._side_at(x)
        # Hover selects the side for rating, but only in single-select mode.
        # why: Ctrl+click sets _both_selected; hover must not clobber that.
        if not self._both_selected:
            self._selected_side = side
        path = self._paths[side] or ""
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="compare_view",
            timestamp=time.time(),
            message=path,
            section=StatusSection.FILEPATH,
        ))

    def leaveEvent(self, event) -> None:
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="compare_view",
            timestamp=time.time(),
            message="",
            section=StatusSection.FILEPATH,
        ))


# ---------------------------------------------------------------------------
# CompareView — container that switches between grid mode and split-line mode
# ---------------------------------------------------------------------------

class CompareView(QWidget):
    escapePressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self._grid_layout = QGridLayout(self)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(2)
        self._splits: list[_CompareSplit] = []
        self._split_line_view: _SplitLineView | None = None
        self._mode = "grid"  # "grid" or "split"
        self._image_paths: list[str] = []
        self._syncing = False
        self.service = None
        self._path_to_split: dict[str, _CompareSplit] = {}
        self._overlay_subscribed = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def selected_paths(self) -> list[str]:
        if self._mode == "split" and self._split_line_view:
            return self._split_line_view.selected_paths
        return [s.current_path for s in self._splits if s.selected and s.current_path]

    def set_service(self, service):
        self.service = service

    def toggle_split_line(self) -> None:
        if self._mode == "grid" and len(self._image_paths) == 2:
            self._set_mode("split")
        elif self._mode == "split":
            self._set_mode("grid")

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._teardown_current_mode()
        self._mode = mode
        if mode == "split" and len(self._image_paths) == 2:
            self._setup_split_line()
        else:
            self._mode = "grid"
            self._setup_grid()

    def _teardown_current_mode(self) -> None:
        if self._overlay_subscribed:
            event_system.unsubscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
            self._overlay_subscribed = False
        if self._mode == "grid":
            for split in self._splits:
                split._picture_base.viewStateChanged.disconnect()
                split.cleanup()
                self._grid_layout.removeWidget(split)
                split.deleteLater()
            self._splits.clear()
            self._path_to_split.clear()
        elif self._mode == "split" and self._split_line_view:
            self._split_line_view.cleanup()
            self._grid_layout.removeWidget(self._split_line_view)
            self._split_line_view.deleteLater()
            self._split_line_view = None

    def _setup_split_line(self) -> None:
        self._split_line_view = _SplitLineView(self)
        self._grid_layout.addWidget(self._split_line_view, 0, 0)
        self._split_line_view.load_images(
            self._image_paths[0], self._image_paths[1], self.service
        )
        event_system.subscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
        self._overlay_subscribed = True

    def _setup_grid(self) -> None:
        n = len(self._image_paths)
        if n == 0:
            return
        cols = math.ceil(math.sqrt(n))
        for i, path in enumerate(self._image_paths):
            split = _CompareSplit(self)
            split._picture_base.viewStateChanged.connect(
                lambda state, s=split: self._on_split_navigated(s, state)
            )
            self._splits.append(split)
            self._path_to_split[path] = split
            self._grid_layout.addWidget(split, i // cols, i % cols)
        if self._splits:
            self._splits[0].selected = True
        event_system.subscribe(EventType.THUMBNAIL_OVERLAY, self._on_overlay_event)
        self._overlay_subscribed = True
        for split, path in zip(self._splits, self._image_paths):
            threading.Thread(
                target=self._load_split_image, args=(split, path), daemon=True
            ).start()

    # -- public entry point --------------------------------------------------

    def load_images(self, image_paths: list[str], mode: str = "split"):
        self._teardown_current_mode()
        self._image_paths = list(image_paths)
        if mode == "split" and len(image_paths) == 2:
            self._mode = "split"
            self._setup_split_line()
        else:
            self._mode = "grid"
            self._setup_grid()

    # -- grid mode helpers ---------------------------------------------------

    def _on_split_clicked(self, split: _CompareSplit, modifiers) -> None:
        if modifiers & Qt.ControlModifier:
            split.selected = not split.selected
        else:
            for s in self._splits:
                s.selected = (s is split)

    def _on_split_hovered(self, split: _CompareSplit) -> None:
        for s in self._splits:
            s.selected = (s is split)

    def _on_overlay_event(self, event_data) -> None:
        if self._mode == "split" and self._split_line_view:
            for path in event_data.paths:
                side = self._split_line_view.path_to_side(path)
                if side is None:
                    continue
                if event_data.action == "show":
                    descriptor = OverlayDescriptor(
                        overlay_id=event_data.overlay_id,
                        renderer_name=event_data.renderer_name,
                        params=event_data.params,
                        position=event_data.position,
                        duration=event_data.duration,
                    )
                    self._split_line_view.show_overlay(side, descriptor)
                elif event_data.action == "remove":
                    self._split_line_view.remove_overlay(side, event_data.overlay_id)
        else:
            for path in event_data.paths:
                split = self._path_to_split.get(path)
                if split is None:
                    continue
                if event_data.action == "show":
                    descriptor = OverlayDescriptor(
                        overlay_id=event_data.overlay_id,
                        renderer_name=event_data.renderer_name,
                        params=event_data.params,
                        position=event_data.position,
                        duration=event_data.duration,
                    )
                    split._overlay_manager.show(0, descriptor)
                elif event_data.action == "remove":
                    split._overlay_manager.remove(0, event_data.overlay_id)
                split.update()

    def _load_split_image(self, split: _CompareSplit, path: str):
        try:
            split.fetch_image(path, self.service)
        except Exception as e:  # why: service/plugin errors must not crash the worker thread
            logger.error(f"CompareView: failed to load {path}: {e}", exc_info=True)

    @property
    def _master(self) -> _CompareSplit | None:
        return self._splits[0] if self._splits else None

    def _on_split_navigated(self, sender: _CompareSplit, state: ViewState):
        if self._syncing or not sender.user_navigating:
            return
        self._syncing = True

        if sender is self._master:
            for split in self._splits[1:]:
                split.interacting = sender.interacting
                split.apply_view_state(
                    QPointF(state.center.x() + split.center_offset.x(),
                            state.center.y() + split.center_offset.y()),
                    state.zoom * split.zoom_ratio,
                    state.fit_mode,
                )
        else:
            # Client navigated by user → update its offset relative to master
            master = self._master
            if master:
                master_state = master.view_state()
                sender.center_offset = QPointF(
                    state.center.x() - master_state.center.x(),
                    state.center.y() - master_state.center.y(),
                )
                if master_state.zoom > 0:
                    sender.zoom_ratio = state.zoom / master_state.zoom

        self._syncing = False

    def _finish_interaction(self, source: _CompareSplit):
        for split in self._splits:
            if split is not source:
                split.interacting = False
                split.update()

    # -- zoom delegation -----------------------------------------------------

    def zoom_in(self, factor: float = 1.25):
        if self._mode == "split" and self._split_line_view:
            self._split_line_view.zoom_in(factor)
        elif self._splits:
            self._splits[0].zoom_in(factor)

    def zoom_out(self, factor: float = 1.25):
        if self._mode == "split" and self._split_line_view:
            self._split_line_view.zoom_out(factor)
        elif self._splits:
            self._splits[0].zoom_out(factor)

    # -- key handling --------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()

    def closeEvent(self, event):
        self._teardown_current_mode()
        super().closeEvent(event)
