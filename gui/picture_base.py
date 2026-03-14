from dataclasses import dataclass
from typing import Optional
from PySide6.QtCore import QObject, Signal, QPointF, QSizeF, QRectF, Qt
from PySide6.QtGui import QImage, QTransform
import logging
import math
logger = logging.getLogger(__name__)
from gui.color_profile import apply_profile

# why: perceptually uniform zoom — each 120 wheel-units (one mouse notch)
# shifts log-zoom by WHEEL_ZOOM_STEP; drag zoom maps pixels at the same rate.
WHEEL_ZOOM_STEP = 0.15
_DRAG_PX_PER_LOG_UNIT = 200.0

@dataclass
class ViewState:
    center: QPointF  # Center point in normalized coordinates (0-1)
    zoom: float      # Current zoom level (1.0 = 100%)
    viewport_size: QSizeF  # Current viewport size in pixels
    fit_mode: bool   # Whether the image should always fit in the viewport

class PictureBase(QObject):

    _MIN_ZOOM = 0.01
    _MAX_ZOOM = 50.0

    # why: only simple rotations supported; mirror orientations (2, 4, 5, 7) are
    # rare in camera output and silently default to 0 (no rotation).
    EXIF_ORIENTATION_DEGREES = {1: 0, 3: 180, 6: 90, 8: 270}

    # Signals for state changes
    viewStateChanged = Signal(ViewState)
    imageLoaded = Signal()

    def __init__(self):
        super().__init__()
        self._raw_image: Optional[QImage] = None
        self._image: Optional[QImage] = None
        self._orientation_degrees: int = 0
        self._view_state = ViewState(
            center=QPointF(0.5, 0.5),
            zoom=1.0,
            viewport_size=QSizeF(0, 0),
            fit_mode=True
        )
        self._padding_rect = QRectF()

        self._is_drag_zooming = False
        self._drag_zoom_anchor = QPointF()
        self._drag_zoom_start_pos = QPointF()
        self._drag_zoom_initial_zoom = 1.0
        self._drag_zoom_initial_center = QPointF(0.5, 0.5)

        self._cached_transform: Optional[QTransform] = None
        self._transform_dirty = True

    def has_image(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def get_image(self) -> Optional[QImage]:
        return self._image if self.has_image() else None

    def setImage(self, image: QImage) -> None:
        image = apply_profile(image)
        self._raw_image = image
        self._orientation_degrees = 0
        self._image = image
        if image and not image.isNull():
            self._updatePaddingRect()
            self.imageLoaded.emit()

    def _apply_orientation(self) -> None:
        # why: always re-derives from _raw_image to avoid cumulative interpolation loss.
        if not self._raw_image or self._raw_image.isNull():
            return
        if self._orientation_degrees == 0:
            self._image = self._raw_image
        else:
            self._image = self._raw_image.transformed(
                QTransform().rotate(self._orientation_degrees),
                Qt.SmoothTransformation,
            )
        self._updatePaddingRect()

    def setOrientation(self, degrees: int) -> None:
        degrees = degrees % 360
        if degrees == self._orientation_degrees:
            return
        self._orientation_degrees = degrees
        self._apply_orientation()
        if self._view_state.fit_mode:
            self._view_state.zoom = self.calculateFitZoom()
            self._view_state.center = QPointF(0.5, 0.5)
        self._transform_dirty = True
        self.viewStateChanged.emit(self._view_state)

    def setOrientationFromExif(self, exif_value: int) -> None:
        self.setOrientation(self.EXIF_ORIENTATION_DEGREES.get(exif_value, 0))

    def rotateBy(self, degrees: int) -> None:
        self.setOrientation(self._orientation_degrees + degrees)

    def orientation(self) -> int:
        return self._orientation_degrees

    def loadImageFromPath(self, path_to_load: str) -> bool:
        image = QImage(path_to_load)
        if not image.isNull():
            self.setImage(image)
            logger.debug("Loaded image: %s", path_to_load)
            return True
        return False

    def loadImageFromBytes(self, data: bytes) -> bool:
        image = QImage()
        if image.loadFromData(data) and not image.isNull():
            self.setImage(image)
            logger.debug("Loaded image from %d bytes", len(data))
            return True
        return False

    def _updatePaddingRect(self) -> None:
        if not self._image:
            return

        img_width = self._image.width()
        img_height = self._image.height()
        square_size = max(img_width, img_height)
        x_padding = (square_size - img_width) / 2
        y_padding = (square_size - img_height) / 2
        self._padding_rect = QRectF(
            -x_padding, -y_padding,
            square_size, square_size
        )
        self._transform_dirty = True


    def screenToNormalized(self, screen_pos: QPointF) -> QPointF:
        """Convert screen coordinates to normalized coordinates (0-1) within the square padded space."""
        if not self._image or self.viewportSize().isEmpty():
            return QPointF()

        transform = self.calculateTransform()
        inv_transform, invertible = transform.inverted()
        if not invertible:
            return QPointF()
        padded_space_pos = inv_transform.map(screen_pos)
        norm_x = (padded_space_pos.x() - self._padding_rect.left()) / self._padding_rect.width()
        # why: Y is flipped to match normalizedToScreen's (1.0 - norm_pos.y()) convention
        norm_y = 1.0 - ((padded_space_pos.y() - self._padding_rect.top()) / self._padding_rect.height())

        result = QPointF(norm_x, norm_y)

        logger.debug("screen coordinates: %s -> padded_space_pos: %s -> normalized: %s", screen_pos, padded_space_pos, result)
        return result

    def normalizedToScreen(self, norm_pos: QPointF) -> QPointF:
        """Convert normalized coordinates (0-1) to screen coordinates."""
        if not self._image or self.viewportSize().isEmpty():
            return QPointF()

        padded_pos = QPointF(
            self._padding_rect.left() + norm_pos.x() * self._padding_rect.width(),
            self._padding_rect.top() + (1.0 - norm_pos.y()) * self._padding_rect.height()
        )
        return self.calculateTransform().map(padded_pos)

    def calculateTransform(self) -> QTransform:
        """Return the transform from image space to screen space (cached)."""
        if not self._transform_dirty and self._cached_transform is not None:
            return self._cached_transform

        transform = QTransform()

        if not self._image or self.viewportSize().isEmpty():
            self._cached_transform = transform
            self._transform_dirty = False
            return transform

        viewport = self.viewportSize()
        transform.translate(viewport.width() / 2, viewport.height() / 2)
        transform.scale(self._view_state.zoom, self._view_state.zoom)
        center_x = self._padding_rect.left() + self._view_state.center.x() * self._padding_rect.width()
        center_y = self._padding_rect.top() + (1.0 - self._view_state.center.y()) * self._padding_rect.height()
        transform.translate(-center_x, -center_y)

        self._cached_transform = transform
        self._transform_dirty = False
        return transform

    def zoomAtAnchor(self, new_zoom: float, anchor_norm: QPointF) -> None:
        """Zoom so that the point under *anchor_norm* stays at the same screen pixel."""
        self._zoom_at_anchor_from(
            new_zoom, anchor_norm,
            self._view_state.zoom, self._view_state.center,
        )

    def _zoom_at_anchor_from(
        self, new_zoom: float, anchor_norm: QPointF,
        old_zoom: float, old_center: QPointF,
    ) -> None:
        if not self._image or new_zoom <= 0:
            return

        new_zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, new_zoom))
        pr = self._padding_rect

        ax = pr.left() + anchor_norm.x() * pr.width()
        ay = pr.top() + (1.0 - anchor_norm.y()) * pr.height()

        cx = pr.left() + old_center.x() * pr.width()
        cy = pr.top() + (1.0 - old_center.y()) * pr.height()

        # why: keeps the anchor pixel fixed on screen while zoom changes.
        #   (anchor - old_center) * old_zoom == (anchor - new_center) * new_zoom
        new_cx = ax - (ax - cx) * old_zoom / new_zoom
        new_cy = ay - (ay - cy) * old_zoom / new_zoom

        norm_x = (new_cx - pr.left()) / pr.width()
        norm_y = 1.0 - (new_cy - pr.top()) / pr.height()

        self.setZoom(new_zoom, QPointF(norm_x, norm_y))

    def _clamp_center(self, center: QPointF) -> QPointF:
        if not self._image or self._view_state.viewport_size.isEmpty():
            return center

        zoom = self._view_state.zoom
        vp_w = self._view_state.viewport_size.width()
        vp_h = self._view_state.viewport_size.height()
        pr = self._padding_rect

        cx = pr.left() + center.x() * pr.width()
        cy = pr.top() + (1.0 - center.y()) * pr.height()

        half_vp_x = vp_w / (2.0 * zoom)
        half_vp_y = vp_h / (2.0 * zoom)

        img_w = self._image.width()
        img_h = self._image.height()

        # why: if image fits in viewport, lock to center; otherwise clamp to edges.
        if img_w <= 2 * half_vp_x:
            cx = img_w / 2.0
        else:
            cx = max(half_vp_x, min(img_w - half_vp_x, cx))

        if img_h <= 2 * half_vp_y:
            cy = img_h / 2.0
        else:
            cy = max(half_vp_y, min(img_h - half_vp_y, cy))

        norm_x = (cx - pr.left()) / pr.width()
        norm_y = 1.0 - (cy - pr.top()) / pr.height()
        return QPointF(norm_x, norm_y)

    def setZoom(self, zoom: float, center: Optional[QPointF] = None) -> None:
        if zoom <= 0:
            return

        zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, zoom))
        self._view_state.zoom = zoom
        if center is not None:
            self._view_state.center = center
        self._view_state.center = self._clamp_center(self._view_state.center)
        self._view_state.fit_mode = False
        self._transform_dirty = True

        self.viewStateChanged.emit(self._view_state)

    def setCenter(self, center: QPointF) -> None:
        center = self._clamp_center(center)
        current = self._view_state.center
        if center.x() == current.x() and center.y() == current.y():
            return
        self._view_state.center = center
        self._view_state.fit_mode = False
        self._transform_dirty = True
        self.viewStateChanged.emit(self._view_state)

    def setViewportSize(self, size: QSizeF) -> None:
        if size != self._view_state.viewport_size:
            self._view_state.viewport_size = size
            if self._view_state.fit_mode:
                self._view_state.zoom = self.calculateFitZoom()
                self._view_state.center = QPointF(0.5, 0.5)
            self._transform_dirty = True
            self.viewStateChanged.emit(self._view_state)

    def viewState(self) -> ViewState:
        return self._view_state

    def viewportSize(self) -> QSizeF:
        return self._view_state.viewport_size

    def imageRect(self) -> QRectF:
        if not self._image:
            return QRectF()
        return QRectF(0, 0, self._image.width(), self._image.height())

    def paddedRect(self) -> QRectF:
        return self._padding_rect

    def calculateFitZoom(self) -> float:
        if not self._image or self.viewportSize().isEmpty():
            return 1.0

        viewport = self.viewportSize()

        zoom_x = viewport.width() / self._image.width()
        zoom_y = viewport.height() / self._image.height()

        return min(zoom_x, zoom_y)

    def setFitMode(self, fit_mode: bool) -> None:
        self._view_state.fit_mode = fit_mode
        if fit_mode:
            self._view_state.zoom = self.calculateFitZoom()
            self._view_state.center = QPointF(0.5, 0.5)
        self._transform_dirty = True
        self.viewStateChanged.emit(self._view_state)

    def isFitMode(self) -> bool:
        return self._view_state.fit_mode

    def zoomIn(self, factor: float = 1.25, center: Optional[QPointF] = None):
        new_zoom = self._view_state.zoom * factor
        c = center if center else self._view_state.center
        self.setZoom(new_zoom, c)

    def zoomOut(self, factor: float = 1.25, center: Optional[QPointF] = None):
        new_zoom = self._view_state.zoom / factor
        c = center if center else self._view_state.center
        self.setZoom(new_zoom, c)

    def zoomToFit(self):
        self.setFitMode(True)

    def zoomReset(self, center: Optional[QPointF] = None):
        c = center if center else QPointF(0.5, 0.5)
        self.setZoom(1.0, c)

    def startDragZoom(self, anchor_point: QPointF, start_position: QPointF):
        self._is_drag_zooming = True
        self._drag_zoom_anchor = anchor_point
        self._drag_zoom_start_pos = start_position
        self._drag_zoom_initial_zoom = self._view_state.zoom
        self._drag_zoom_initial_center = QPointF(self._view_state.center)

    def updateDragZoom(self, current_position: QPointF):
        if not self._is_drag_zooming:
            return
        delta_x = current_position.x() - self._drag_zoom_start_pos.x()
        log_zoom = math.log(self._drag_zoom_initial_zoom) + delta_x / _DRAG_PX_PER_LOG_UNIT
        new_zoom = math.exp(log_zoom)
        self._zoom_at_anchor_from(
            new_zoom, self._drag_zoom_anchor,
            self._drag_zoom_initial_zoom, self._drag_zoom_initial_center,
        )

    def endDragZoom(self):
        self._is_drag_zooming = False

    def computeDragZoom(self, current_pos: QPointF) -> Optional[float]:
        if not self._is_drag_zooming:
            return None
        delta_x = current_pos.x() - self._drag_zoom_start_pos.x()
        new_zoom = math.exp(math.log(self._drag_zoom_initial_zoom) + delta_x / _DRAG_PX_PER_LOG_UNIT)
        return new_zoom if new_zoom > 0 else None

    def isDragZooming(self) -> bool:
        return self._is_drag_zooming

    def resetDragZoom(self) -> None:
        self._is_drag_zooming = False

    def cleanup_subscriptions(self) -> None:
        # why: kept as a no-op for callers that still call it during teardown.
        pass
