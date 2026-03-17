import enum
import logging
import threading

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QPointF, QPoint, Signal, Slot
from PySide6.QtGui import QPainter, QImage

from gui.picture_base import PictureBase

logger = logging.getLogger(__name__)

_MIN_ZOOM = 0.1
_MAX_ZOOM = 20.0


class ViewMode(enum.Enum):
    TRACKING = "tracking"
    FIT = "fit"
    MANUAL = "manual"


class ImageInspector(QWidget):
    view_mode_changed = Signal()
    # Delivers background-thread fetch results back to the GUI thread.
    _preview_status_ready = Signal(str, str, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._picture_base = PictureBase()
        self._current_image_path: str | None = None
        self._view_image_ready = False
        self._is_panning = False
        self._last_mouse_pos = QPoint()

        self._desired_image_path: str | None = None
        self._desired_norm_pos: QPointF = QPointF(0.5, 0.5)
        self._fetch_in_flight: str | None = None
        # why: CPython GIL makes single bool read/write atomic; the worst case is
        # one extra signal emission after close, which Qt discards safely.
        self._fetch_cancelled = False

        self._zoom_factor: float = 3.0
        self._view_mode: ViewMode = ViewMode.TRACKING

        self._picture_base.viewStateChanged.connect(self.update)
        self._preview_status_ready.connect(self._on_preview_status_ready)

        self._picture_base.setZoom(self._zoom_factor)

        self.service = None

    @property
    def current_path(self) -> str | None:
        return self._current_image_path

    @property
    def view_mode(self) -> ViewMode:
        return self._view_mode

    @view_mode.setter
    def view_mode(self, mode: ViewMode):
        self._view_mode = mode
        self.view_mode_changed.emit()

    @property
    def zoom_factor(self) -> float:
        return self._zoom_factor

    def set_zoom_factor(self, zoom: float):
        # why: tighter bounds than PictureBase (0.01–50.0) because the inspector
        # window is small; sub-0.1x is invisible and >20x is pixelated noise.
        self._zoom_factor = max(_MIN_ZOOM, min(zoom, _MAX_ZOOM))
        self.view_mode_changed.emit()
        # why: defer PictureBase sync until an image is loaded; update_image() applies
        # self._zoom_factor when the next image loads, keeping state consistent.
        if self._current_image_path:
            if self._view_mode == ViewMode.FIT:
                self._view_mode = ViewMode.TRACKING
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)

    def zoom_by_factor(self, factor: float):
        if self._view_mode == ViewMode.FIT:
            self._view_mode = ViewMode.TRACKING
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)
            self.view_mode_changed.emit()
        self.set_zoom_factor(self._zoom_factor * factor)

    def set_center(self, norm_pos: QPointF):
        if self._current_image_path:
            # why: setCenter emits viewStateChanged → self.update; explicit update() is redundant.
            self._picture_base.setCenter(norm_pos)

    def cancel_fetches(self):
        self._fetch_cancelled = True

    def reset_fetches(self):
        self._fetch_cancelled = False

    # ------------------------------------------------------------------ image loading

    def handle_update(self, image_path: str, norm_pos: QPointF, cache_only: bool = False):
        self._desired_image_path = image_path
        self._desired_norm_pos = norm_pos

        same_image = image_path == self._current_image_path
        if not same_image:
            self._view_image_ready = False

        # why: skip fetch if image already loaded; _view_image_ready stays True
        # until a different image is hovered.
        if same_image and self._view_image_ready:
            if self._view_mode == ViewMode.TRACKING:
                self.set_center(norm_pos)
            return

        if not self.service:
            return

        if self._fetch_in_flight == image_path:
            return

        if not same_image and self._current_image_path is not None:
            self._picture_base.setImage(QImage())

        self._fetch_in_flight = image_path
        threading.Thread(
            target=self._fetch_preview_status,
            args=(image_path, norm_pos, cache_only),
            daemon=True,
            name="inspector-fetch",
        ).start()

    def handle_previews_ready(self, path: str, view_image_path: str, view_image_source: str, norm_pos: QPointF):
        is_target = (path == self._current_image_path or path == self._desired_image_path)
        view_ready = view_image_path or view_image_source == "memory"
        if not (view_ready and is_target):
            return
        if view_image_source == "memory":
            self._load_mem_cached_view(path, norm_pos)
        else:
            self._update_view(path, view_image_path, norm_pos)

    def _fetch_preview_status(self, image_path: str, norm_pos: QPointF, cache_only: bool = False):
        try:
            response = self.service.get_previews_status([image_path])
            view_image_path = ""
            if response:
                status = response.get(image_path, {})
                if status.get('view_image_ready') and status.get('view_image_path'):
                    view_image_path = status['view_image_path']
                # Fallback to thumbnail when fullres is unavailable
                elif cache_only and status.get('thumbnail_ready') and status.get('thumbnail_path'):
                    view_image_path = status['thumbnail_path']

            if not view_image_path and not cache_only:
                result = self.service.request_view_image(image_path)
                if result:
                    if result.get('view_image_source') == "memory":
                        view_image_path = "memory"
                    elif result.get('view_image_path'):
                        view_image_path = result['view_image_path']
        except Exception as e:
            # why: service calls can raise on NAS drop or thread errors;
            # log and emit empty path to unblock GUI.
            logger.error("ImageInspector: error fetching preview for %s: %s", image_path, e)
            view_image_path = ""

        if not self._fetch_cancelled:
            self._preview_status_ready.emit(image_path, view_image_path, norm_pos)

    @Slot(str, str, QPointF)
    def _on_preview_status_ready(self, image_path: str, view_image_path: str, norm_pos: QPointF):
        if self._fetch_in_flight == image_path:
            self._fetch_in_flight = None

        if image_path != self._desired_image_path:
            return

        if not view_image_path:
            return

        if view_image_path == "memory":
            self._load_mem_cached_view(image_path, norm_pos)
            return

        if self._view_mode != ViewMode.TRACKING:
            if image_path != self._current_image_path:
                self._update_view(image_path, view_image_path, QPointF(0.5, 0.5))
        else:
            self._update_view(image_path, view_image_path, norm_pos)

    def _load_mem_cached_view(self, image_path: str, norm_pos: QPointF):
        if not self.service:
            return
        image_bytes = self.service.get_cached_view_image(image_path)
        if not image_bytes:
            return
        if image_path != self._current_image_path:
            success = self._picture_base.loadImageFromBytes(image_bytes)
            if success:
                self._apply_orientation(image_path)
                self._current_image_path = image_path
                self._view_image_ready = True
                self._picture_base.setViewportSize(self.size())
                if self._view_mode == ViewMode.FIT:
                    self._picture_base.setFitMode(True)
                else:
                    self._picture_base.setZoom(self._zoom_factor)
        if self._view_mode == ViewMode.TRACKING:
            self.set_center(norm_pos)

    def _update_view(self, original_image_path: str, view_image_path: str, norm_pos: QPointF):
        if not original_image_path:
            return

        if original_image_path != self._current_image_path:
            success = self._picture_base.loadImageFromPath(view_image_path)
            if success:
                self._apply_orientation(original_image_path)
                self._current_image_path = original_image_path
                self._view_image_ready = True
                self._picture_base.setViewportSize(self.size())
                if self._view_mode == ViewMode.FIT:
                    self._picture_base.setFitMode(True)
                else:
                    self._picture_base.setZoom(self._zoom_factor)
            else:
                self._current_image_path = None
                logger.warning("ImageInspector failed to load: %s", original_image_path)
                return

        if self._view_mode == ViewMode.TRACKING:
            self.set_center(norm_pos)

    def _apply_orientation(self, image_path: str) -> None:
        if not self.service:
            return
        try:
            resp = self.service.get_metadata_batch([image_path])
            orientation = resp.get(image_path, {}).get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            return
        self._picture_base.setOrientationFromExif(orientation)

    # ------------------------------------------------------------------ rendering

    def paintEvent(self, event):
        if not self._current_image_path or not self._picture_base.has_image():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), Qt.black)
        # why: viewport size is kept current by resizeEvent; no need to sync here
        # (doing so inside paintEvent emits viewStateChanged → schedules a second repaint)
        transform = self._picture_base.calculateTransform()
        painter.setTransform(transform)
        image_rect = self._picture_base.imageRect()
        painter.drawImage(image_rect, self._picture_base.get_image())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._current_image_path:
            self._picture_base.setViewportSize(self.size())
            if self._view_mode == ViewMode.FIT:
                self._picture_base.setFitMode(True)

    # ------------------------------------------------------------------ mouse

    def mousePressEvent(self, event):
        if self._view_mode == ViewMode.FIT:
            return
        # why: do NOT call _enter_manual_mode() here — Qt fires mousePressEvent
        # before mouseDoubleClickEvent, so entering manual on press would prevent
        # double-click from toggling back to tracking. Manual mode is entered on
        # the first actual drag in mouseMoveEvent instead.
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
            zoom_anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(zoom_anchor, QPointF(event.position()))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._view_mode == ViewMode.FIT:
            return
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._view_mode == ViewMode.FIT:
            return

        if self._is_panning:
            if self._view_mode != ViewMode.MANUAL:
                self._view_mode = ViewMode.MANUAL
                self.view_mode_changed.emit()
            delta = event.position().toPoint() - self._last_mouse_pos
            self._last_mouse_pos = event.position().toPoint()

            transform = self._picture_base.calculateTransform()
            inv_transform, invertible = transform.inverted()
            if invertible:
                delta_normalized = inv_transform.map(QPointF(delta)) - inv_transform.map(QPointF(0, 0))

                current_center = self._picture_base.viewState().center
                new_center = QPointF(
                    current_center.x() - delta_normalized.x() / self._picture_base.paddedRect().width(),
                    current_center.y() + delta_normalized.y() / self._picture_base.paddedRect().height()
                )
                self._picture_base.setCenter(new_center)
        elif self._picture_base.isDragZooming():
            new_zoom = self._picture_base.computeDragZoom(event.position())
            if new_zoom is not None:
                self.set_zoom_factor(new_zoom)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._view_mode == ViewMode.FIT:
                self._view_mode = ViewMode.TRACKING
                self._picture_base.setFitMode(False)
                self._picture_base.setZoom(self._zoom_factor)
            else:
                self._view_mode = ViewMode.FIT
                self._picture_base.setFitMode(True)
            self.view_mode_changed.emit()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if self._view_mode == ViewMode.FIT:
            self._view_mode = ViewMode.TRACKING
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)
            self.view_mode_changed.emit()
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        self.set_zoom_factor(self._zoom_factor * factor)
        event.accept()
