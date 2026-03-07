from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, Slot, QPointF, QSizeF, QPoint, QTimer
from PySide6.QtGui import QPainter, QImage, QMouseEvent, QPaintEvent, QResizeEvent, QKeyEvent, QTransform

import logging
logger = logging.getLogger(__name__)
import os
import time
import threading
from .picture_base import PictureBase
from core.event_system import event_system, EventType, InspectorEventData, StatusMessageEventData, StatusSection
from network.daemon_signals import DaemonSignals
from core.notifications import PreviewsReadyData

class PictureView(QWidget):

    # Signals
    escapePressed = Signal()  # Signal for when Escape is pressed
    zoomChanged = Signal(float)  # Signal with new zoom level
    imageChanged = Signal(str)  # Signal emitted when current image changes
    closeRequested = Signal()
    _rating_ready = Signal(str, int)  # (path, rating) — marshalled from bg thread
    
    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        
        self.config_manager = config_manager
        self._current_path = None # This will store the ORIGINAL path
        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self.update)
        self._rating_ready.connect(self._on_rating_ready)
        self._daemon_signals: DaemonSignals | None = None
        
        self._is_panning = False
        self._last_mouse_pos = QPoint()

        self.service = None

    def set_service(self, service):
        self.service = service

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()

    def _updateInspector(self, event_pos: QPointF) -> None:
        if not self._current_path or not self._picture_base.has_image():
            return
            
        try:
            # Convert to normalized coordinates using PictureBase
            norm_pos = self._picture_base.screenToNormalized(event_pos)
            
            if 0 <= norm_pos.x() <= 1 and 0 <= norm_pos.y() <= 1:
                # Publish inspector update event
                event_data = InspectorEventData(
                    event_type=EventType.INSPECTOR_UPDATE,
                    source="picture_view",
                    timestamp=time.time(),
                    image_path=self._current_path,
                    normalized_position=norm_pos
                )
                event_system.publish(event_data)
                logger.debug(f"Published inspector event from picture view: {self._current_path} at {norm_pos.x():.2f}, {norm_pos.y():.2f}")
                    
        except Exception as e:  # why: called from mouse events; geometry errors must not crash the widget
            logger.error(f"Error updating inspector in picture view: {e}", exc_info=True)

    def loadImage(self, image_path: str, force_reload: bool = False) -> bool:
        """Load an image from the given path, preferring full resolution cached version."""
        # image_path here is always the ORIGINAL path
        if image_path == self._current_path and not force_reload:
            return True  # Already loaded, and not forced to reload
        
        if not self.service:
            logger.error("Service not initialized in PictureView.")
            return False

        # Capture current view state before loading so we can restore it
        _prev_fit = self._picture_base.isFitMode()
        _prev_center = QPointF(self._picture_base.viewState().center)
        _prev_fit_zoom = self._picture_base.calculateFitZoom()
        _prev_zoom = self._picture_base.viewState().zoom
        
        # Request the view image at FULLRES_REQUEST priority. Always returns JSON.
        # view_image_source="memory" → bytes available via get_cached_view_image.
        # view_image_path set → disk-cached. Neither → generation queued.
        result = self.service.request_view_image(image_path)

        if result is None:
            logger.warning(f"Failed to request view image (comm failure), will retry: {image_path}")
            self._picture_base.setImage(QImage())
            self._current_path = image_path
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="picture_view",
                timestamp=time.time(),
                message="Connecting...",
                section=StatusSection.PROCESS,
            ))
            QTimer.singleShot(1000, lambda p=image_path: self._retry_load(p))
            return False

        if result.get('view_image_source') == "memory":
            # Mem-cached on daemon — fetch raw bytes via dedicated call.
            image_bytes = self.service.get_cached_view_image(image_path)
            success = self._picture_base.loadImageFromBytes(image_bytes) if image_bytes else False
        elif result.get('view_image_path'):
            # Disk-cached — load from path.
            success = self._picture_base.loadImageFromPath(result['view_image_path'])
        else:
            # Generation queued — show placeholder and wait for previews_ready notification.
            self._picture_base.setImage(QImage())  # Clear the view
            self._current_path = image_path  # Set path so notification handler knows what to load
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="picture_view",
                timestamp=time.time(),
                message=image_path,
                section=StatusSection.FILEPATH,
            ))
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="picture_view",
                timestamp=time.time(),
                message=f"Generating preview for {os.path.basename(image_path)}...",
                section=StatusSection.PROCESS,
            ))
            return False  # Indicate loading is in progress

        if success:
            # Apply DB orientation as a visual transform (rotation is metadata-only).
            self._apply_db_orientation(image_path)

            self._current_path = image_path  # Store original path for navigation and external use
            self.imageChanged.emit(self._current_path)  # Emit signal with original path

            # Update filepath section immediately
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="picture_view",
                timestamp=time.time(),
                message=image_path,
                section=StatusSection.FILEPATH,
            ))

            # Fetch rating off the GUI thread to avoid blocking on slow daemon responses
            threading.Thread(
                target=self._fetch_rating, args=(image_path,), daemon=True
            ).start()
            
            if _prev_fit or _prev_fit_zoom <= 0:
                self._picture_base.setFitMode(True)
            else:
                new_fit_zoom = self._picture_base.calculateFitZoom()
                relative_zoom = _prev_zoom / _prev_fit_zoom
                clamped_center = QPointF(
                    max(0.0, min(1.0, _prev_center.x())),
                    max(0.0, min(1.0, _prev_center.y())),
                )
                self._picture_base.setZoom(relative_zoom * new_fit_zoom, clamped_center)

            self._picture_base.resetDragZoom()
            
            self.update()

            # Publish inspector event using the current mouse position so the
            # inspector tracks correctly after image navigation (not just center).
            local_pos = self.mapFromGlobal(self.cursor().pos())
            norm_pos = self._picture_base.screenToNormalized(QPointF(local_pos))
            norm_pos = QPointF(
                max(0.0, min(1.0, norm_pos.x())),
                max(0.0, min(1.0, norm_pos.y())),
            )
            event_data = InspectorEventData(
                event_type=EventType.INSPECTOR_UPDATE,
                source="picture_view",
                timestamp=time.time(),
                image_path=self._current_path,
                normalized_position=norm_pos,
            )
            event_system.publish(event_data)

            return True
        else:
            logger.error(f"Failed to load image: {image_path}")
            return False
        
    @property
    def current_path(self) -> str:
        return self._current_path

    def _fetch_rating(self, path: str):
        """Fetch rating from daemon in a background thread and marshal result to main thread."""
        rating = 0
        if self.service:
            try:
                resp = self.service.get_metadata_batch([path])
                if resp and path in resp:
                    rating = resp[path].get("rating", 0) or 0
            except Exception as e:  # why: service calls can raise errors; emit zero so status bar gets a value
                logger.debug(f"Rating fetch failed for {path}: {e}")
        self._rating_ready.emit(path, int(rating))

    @Slot(str, int)
    def _on_rating_ready(self, path: str, rating: int):
        """Publish rating to status bar if the path is still current."""
        if self._current_path == path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="picture_view",
                timestamp=time.time(),
                message=str(rating),
                section=StatusSection.RATING,
            ))

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._daemon_signals = daemon_signals
        daemon_signals.previews_ready.connect(self._on_previews_ready)

    # EXIF orientation → clockwise rotation degrees (non-mirror orientations).
    _ORIENTATION_DEGREES = {1: 0, 3: 180, 6: 90, 8: 270}

    def _apply_db_orientation(self, image_path: str) -> None:
        """Apply the DB orientation as a visual rotation after loading a cached image."""
        if not self.service:
            return
        orientation = 1
        try:
            resp = self.service.get_metadata_batch([image_path])
            if resp and image_path in resp:
                orientation = resp[image_path].get('orientation', 1) or 1
        except Exception:
            pass
        degrees = self._ORIENTATION_DEGREES.get(orientation, 0)
        if degrees:
            img = self._picture_base.get_image()
            if img and not img.isNull():
                rotated = img.transformed(QTransform().rotate(degrees), Qt.SmoothTransformation)
                self._picture_base.setImage(rotated)

    def rotate_current_image(self, degrees: int) -> None:
        """Apply a visual rotation to the currently displayed image immediately."""
        img = self._picture_base.get_image()
        if img is None or img.isNull():
            return
        rotated = img.transformed(QTransform().rotate(degrees), Qt.SmoothTransformation)
        self._picture_base.setImage(rotated)
        self._picture_base.setFitMode(True)
        self.update()

    @Slot(object)
    def _on_previews_ready(self, data: PreviewsReadyData) -> None:
        # If this is the image we are waiting for, load it.
        view_ready = data.view_image_path or data.view_image_source == "memory"
        if view_ready and data.image_entry.path == self._current_path:
            logger.info(f"Loading newly generated view image via notification: {data.image_entry.path}")
            self.loadImage(data.image_entry.path, force_reload=True)

    def _retry_load(self, image_path: str) -> None:
        """Retry loading an image after a transient comm failure, if still current."""
        if self._current_path == image_path:
            self.loadImage(image_path, force_reload=True)

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._picture_base.has_image():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        transform = self._picture_base.calculateTransform()
        painter.setTransform(transform)

        painter.drawImage(0, 0, self._picture_base.get_image())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # First update inspector
        self._updateInspector(QPointF(event.position()))

        # Then handle standard mouse movement
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
                    current_center.y() + delta_normalized.y() / self._picture_base.paddedRect().height()
                )
                self._picture_base.setCenter(new_center)
                
        elif self._picture_base.isDragZooming():
            self._picture_base.updateDragZoom(QPointF(event.position()))
            self.zoomChanged.emit(self._picture_base.viewState().zoom)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._updateInspector(QPointF(event.position()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            
        elif event.button() == Qt.RightButton:
            zoom_anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(zoom_anchor, QPointF(event.position()))
            
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self.setCursor(Qt.ArrowCursor)
            
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
            
                
    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
            
            # PictureView specific double click behavior: toggle between fit mode and 100% zoom
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                # Switch to 100% zoom at click position
                self._picture_base.setZoom(1.0, click_pos)
            else:
                # Switch to fit mode
                self._picture_base.setFitMode(True)
            
            self.zoomChanged.emit(self._picture_base.viewState().zoom)
                
                
    def wheelEvent(self, event) -> None:
        factor = 1.25 if event.angleDelta().y() > 0 else 1/1.25
        mouse_pos = self._picture_base.screenToNormalized(QPointF(event.position()))

        if event.angleDelta().y() > 0:
            # Zoom in using PictureBase
            self._picture_base.zoomIn(factor, mouse_pos)
        else:
            # Zoom out using PictureBase
            self._picture_base.zoomOut(factor, mouse_pos)

        self.zoomChanged.emit(self._picture_base.viewState().zoom)

    def has_image(self) -> bool:
        return self._picture_base.has_image()

    def screen_to_normalized(self, pos: QPointF) -> QPointF:
        return self._picture_base.screenToNormalized(pos)

    def zoom_in(self, factor: float = 1.25):
        self._picture_base.zoomIn(factor)

    def zoom_out(self, factor: float = 1.25):
        self._picture_base.zoomOut(factor)

    def closeEvent(self, event):
        if self._daemon_signals:
            self._daemon_signals.previews_ready.disconnect(self._on_previews_ready)
        super().closeEvent(event)
