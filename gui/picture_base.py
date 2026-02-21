from dataclasses import dataclass
from typing import Optional
from PySide6.QtCore import QObject, Signal, QPointF, QSizeF, QRectF
from PySide6.QtGui import QImage, QTransform
import logging
from core.event_system import event_system, EventType, ZoomEventData, ZoomDragEventData, DoubleClickZoomEventData
import time

@dataclass
class ViewState:
    """Represents the current view state of the image."""
    center: QPointF  # Center point in normalized coordinates (0-1)
    zoom: float      # Current zoom level (1.0 = 100%)
    viewport_size: QSizeF  # Current viewport size in pixels
    fit_mode: bool   # Whether the image should always fit in the viewport

class PictureBase(QObject):
    """Base class for image viewing widgets with normalized coordinate system."""

    _DRAG_ZOOM_THRESHOLD = 10
    _MIN_ZOOM = 0.01
    _MAX_ZOOM = 50.0

    # Signals for state changes
    viewStateChanged = Signal(ViewState)
    imageLoaded = Signal()
    
    def __init__(self):
        super().__init__()
        self._image: Optional[QImage] = None
        self._view_state = ViewState(
            center=QPointF(0.5, 0.5),  # Start at center
            zoom=1.0,
            viewport_size=QSizeF(0, 0),
            fit_mode=True  # Start in fit mode
        )
        self._padding_rect = QRectF()

        self._is_drag_zooming = False
        self._drag_zoom_anchor = QPointF()
        self._drag_zoom_start_pos = QPointF()
        self._drag_zoom_initial_zoom = 1.0
        
        self._setup_event_subscriptions()

    def _setup_event_subscriptions(self):
        event_system.subscribe(EventType.ZOOM_IN, self._handle_zoom_in)
        event_system.subscribe(EventType.ZOOM_OUT, self._handle_zoom_out)
        event_system.subscribe(EventType.ZOOM_TO_POINT, self._handle_zoom_to_point)
        event_system.subscribe(EventType.ZOOM_FIT, self._handle_zoom_fit)
        event_system.subscribe(EventType.ZOOM_RESET, self._handle_zoom_reset)
        event_system.subscribe(EventType.ZOOM_DRAG_START, self._handle_zoom_drag_start)
        event_system.subscribe(EventType.ZOOM_DRAG_UPDATE, self._handle_zoom_drag_update)
        event_system.subscribe(EventType.ZOOM_DRAG_END, self._handle_zoom_drag_end)
        event_system.subscribe(EventType.DOUBLE_CLICK_ZOOM, self._handle_double_click_zoom)

    def _handle_zoom_in(self, event_data: ZoomEventData):
        if event_data.source != id(self):
            return
            
        current_zoom = self._view_state.zoom
        new_zoom = current_zoom * event_data.zoom_factor
        center = event_data.center_point if event_data.center_point else self._view_state.center
        self.setZoom(new_zoom, center)

    def _handle_zoom_out(self, event_data: ZoomEventData):
        if event_data.source != id(self):
            return
            
        current_zoom = self._view_state.zoom
        new_zoom = current_zoom / event_data.zoom_factor
        center = event_data.center_point if event_data.center_point else self._view_state.center
        self.setZoom(new_zoom, center)

    def _handle_zoom_to_point(self, event_data: ZoomEventData):
        if event_data.source != id(self):
            return
            
        center = event_data.center_point if event_data.center_point else QPointF(0.5, 0.5)
        self.setZoom(event_data.zoom_factor, center)

    def _handle_zoom_fit(self, event_data: ZoomEventData):
        if event_data.source != id(self):
            return
            
        self.setFitMode(True)

    def _handle_zoom_reset(self, event_data: ZoomEventData):
        if event_data.source != id(self):
            return
            
        center = event_data.center_point if event_data.center_point else QPointF(0.5, 0.5)
        self.setZoom(1.0, center)

    def _handle_zoom_drag_start(self, event_data: ZoomDragEventData):
        if event_data.source != id(self):
            return
            
        self._is_drag_zooming = True
        self._drag_zoom_anchor = event_data.anchor_point
        self._drag_zoom_start_pos = event_data.start_position
        self._drag_zoom_initial_zoom = event_data.initial_zoom

    def _handle_zoom_drag_update(self, event_data: ZoomDragEventData):
        if event_data.source != id(self) or not self._is_drag_zooming:
            return
            
        delta_x = event_data.current_position.x() - self._drag_zoom_start_pos.x()
        
        if abs(delta_x) < self._DRAG_ZOOM_THRESHOLD:
            return

        adjusted_delta = delta_x - (self._DRAG_ZOOM_THRESHOLD if delta_x > 0 else -self._DRAG_ZOOM_THRESHOLD)
        zoom_change = adjusted_delta / 100.0
        new_zoom = self._drag_zoom_initial_zoom * (1.0 + zoom_change)
        
        if new_zoom > 0:
            self.setZoom(new_zoom, self._drag_zoom_anchor)

    def _handle_zoom_drag_end(self, event_data: ZoomDragEventData):
        if event_data.source != id(self):
            return
            
        self._is_drag_zooming = False

    def _handle_double_click_zoom(self, event_data: DoubleClickZoomEventData):
        if event_data.source != id(self):
            return

        if self._view_state.fit_mode:
            self.setFitMode(False)
            self.setZoom(1.0, event_data.click_position)
        else:
            self.setFitMode(True)

    def has_image(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def get_image(self) -> Optional[QImage]:
        return self._image if self.has_image() else None

    def setImage(self, image: QImage) -> None:
        """Set the image and initialize view state."""
        self._image = image
        if image and not image.isNull():
            self._updatePaddingRect()
            self.imageLoaded.emit()
            
    def loadImageFromPath(self, path_to_load: str) -> bool:
        """Load image from the specified path."""
        image = QImage(path_to_load)
        if not image.isNull():
            self.setImage(image)
            logging.debug(f"Loaded image: {path_to_load}")
            return True
        return False
            
    def _updatePaddingRect(self) -> None:
        """Calculate the padding required to make the image space square."""
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
    
        logging.debug(f"screen coordinates: {screen_pos} -> padded_space_pos: {padded_space_pos} -> normalized: {result}")
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
        """Calculate the transform from image space to screen space."""
        transform = QTransform()
        
        if not self._image or self.viewportSize().isEmpty():
            return transform
            
        viewport = self.viewportSize()
        transform.translate(viewport.width() / 2, viewport.height() / 2)
        transform.scale(self._view_state.zoom, self._view_state.zoom)
        center_x = self._padding_rect.left() + self._view_state.center.x() * self._padding_rect.width()
        center_y = self._padding_rect.top() + (1.0 - self._view_state.center.y()) * self._padding_rect.height()
        transform.translate(-center_x, -center_y)
        
        return transform
        
    def setZoom(self, zoom: float, center: Optional[QPointF] = None) -> None:
        """Set the zoom level and optionally the center point."""
        if zoom <= 0:
            return
            
        zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, zoom))
        self._view_state.zoom = zoom
        if center is not None:
            self._view_state.center = center
        self._view_state.fit_mode = False
            
        self.viewStateChanged.emit(self._view_state)
        
    def setCenter(self, center: QPointF) -> None:
        """Set the center point in normalized coordinates."""
        self._view_state.center = center
        self._view_state.fit_mode = False
        self.viewStateChanged.emit(self._view_state)
        
    def setViewportSize(self, size: QSizeF) -> None:
        """Update the viewport size."""
        if size != self._view_state.viewport_size:
            self._view_state.viewport_size = size
            if self._view_state.fit_mode:
                self._view_state.zoom = self.calculateFitZoom()
                self._view_state.center = QPointF(0.5, 0.5)
            self.viewStateChanged.emit(self._view_state)
            
    def viewState(self) -> ViewState:
        """Get the current view state."""
        return self._view_state
        
    def viewportSize(self) -> QSizeF:
        """Get the current viewport size."""
        return self._view_state.viewport_size
        
    def imageRect(self) -> QRectF:
        """Get the actual image rectangle in padded space."""
        if not self._image:
            return QRectF()
            
        return QRectF(0, 0, self._image.width(), self._image.height())
        
    def paddedRect(self) -> QRectF:
        """Get the padded square rectangle."""
        return self._padding_rect
        
    def calculateFitZoom(self) -> float:
        """Calculate the zoom level needed to fit the image in the viewport."""
        if not self._image or self.viewportSize().isEmpty():
            return 1.0
            
        viewport = self.viewportSize()
        
        # Calculate zoom to fit the actual image dimensions
        zoom_x = viewport.width() / self._image.width()
        zoom_y = viewport.height() / self._image.height()
        
        return min(zoom_x, zoom_y)
    
    def setFitMode(self, fit_mode: bool) -> None:
        """Enable or disable fit mode."""
        self._view_state.fit_mode = fit_mode
        if fit_mode:
            self._view_state.zoom = self.calculateFitZoom()
            self._view_state.center = QPointF(0.5, 0.5)
        self.viewStateChanged.emit(self._view_state)
    
    def isFitMode(self) -> bool:
        """Check if fit mode is enabled."""
        return self._view_state.fit_mode
    
    def zoomIn(self, factor: float = 1.25, center: Optional[QPointF] = None):
        """Zoom in by the specified factor."""
        event_data = ZoomEventData(
            event_type=EventType.ZOOM_IN,
            source=id(self),
            timestamp=time.time(),
            zoom_factor=factor,
            center_point=center
        )
        event_system.publish(event_data)
    
    def zoomOut(self, factor: float = 1.25, center: Optional[QPointF] = None):
        """Zoom out by the specified factor."""
        event_data = ZoomEventData(
            event_type=EventType.ZOOM_OUT,
            source=id(self),
            timestamp=time.time(),
            zoom_factor=factor,
            center_point=center
        )
        event_system.publish(event_data)
    
    def zoomToPoint(self, zoom: float, center: QPointF):
        """Zoom to a specific zoom level at a specific point."""
        event_data = ZoomEventData(
            event_type=EventType.ZOOM_TO_POINT,
            source=id(self),
            timestamp=time.time(),
            zoom_factor=zoom,
            center_point=center
        )
        event_system.publish(event_data)
    
    def zoomToFit(self):
        """Zoom to fit the image in the viewport."""
        event_data = ZoomEventData(
            event_type=EventType.ZOOM_FIT,
            source=id(self),
            timestamp=time.time(),
            zoom_factor=0.0,  # Not used for fit mode
            fit_mode=True
        )
        event_system.publish(event_data)
    
    def zoomReset(self, center: Optional[QPointF] = None):
        """Reset zoom to 100%."""
        event_data = ZoomEventData(
            event_type=EventType.ZOOM_RESET,
            source=id(self),
            timestamp=time.time(),
            zoom_factor=1.0,
            center_point=center
        )
        event_system.publish(event_data)
    
    def startDragZoom(self, anchor_point: QPointF, start_position: QPointF):
        """Start drag zoom operation."""
        event_data = ZoomDragEventData(
            event_type=EventType.ZOOM_DRAG_START,
            source=id(self),
            timestamp=time.time(),
            anchor_point=anchor_point,
            current_position=start_position,
            start_position=start_position,
            initial_zoom=self._view_state.zoom
        )
        event_system.publish(event_data)
    
    def updateDragZoom(self, current_position: QPointF):
        """Update drag zoom operation."""
        if self._is_drag_zooming:
            event_data = ZoomDragEventData(
                event_type=EventType.ZOOM_DRAG_UPDATE,
                source=id(self),
                timestamp=time.time(),
                anchor_point=self._drag_zoom_anchor,
                current_position=current_position,
                start_position=self._drag_zoom_start_pos,
                initial_zoom=self._drag_zoom_initial_zoom
            )
            event_system.publish(event_data)
    
    def endDragZoom(self):
        """End drag zoom operation."""
        if self._is_drag_zooming:
            event_data = ZoomDragEventData(
                event_type=EventType.ZOOM_DRAG_END,
                source=id(self),
                timestamp=time.time(),
                anchor_point=self._drag_zoom_anchor,
                current_position=QPointF(),
                start_position=self._drag_zoom_start_pos,
                initial_zoom=self._drag_zoom_initial_zoom
            )
            event_system.publish(event_data)
    
    def computeDragZoom(self, current_pos: QPointF) -> Optional[float]:
        """Return the new zoom implied by current drag position, or None if below threshold."""
        if not self._is_drag_zooming:
            return None
        delta_x = current_pos.x() - self._drag_zoom_start_pos.x()
        if abs(delta_x) < self._DRAG_ZOOM_THRESHOLD:
            return None
        adjusted_delta = delta_x - (self._DRAG_ZOOM_THRESHOLD if delta_x > 0 else -self._DRAG_ZOOM_THRESHOLD)
        new_zoom = self._drag_zoom_initial_zoom * (1.0 + adjusted_delta / 100.0)
        return new_zoom if new_zoom > 0 else None

    def isDragZooming(self) -> bool:
        """Check if currently drag zooming."""
        return self._is_drag_zooming

    def resetDragZoom(self) -> None:
        self._is_drag_zooming = False
    
