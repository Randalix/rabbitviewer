from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, Slot, QPointF, QSizeF, QPoint
from PySide6.QtGui import QPainter, QImage, QMouseEvent, QPaintEvent, QResizeEvent, QKeyEvent

import logging
import os
import time
import threading
from .picture_base import PictureBase
from core.event_system import event_system, EventType, InspectorEventData, DaemonNotificationEventData, StatusMessageEventData, StatusSection
from network.socket_client import ThumbnailSocketClient
from network import protocol

class PictureView(QWidget):

    # Signals
    escapePressed = Signal()  # Signal for when Escape is pressed
    zoomChanged = Signal(float)  # Signal with new zoom level
    imageChanged = Signal(str)  # Signal emitted when current image changes
    closeRequested = Signal()
    _daemon_notification_received = Signal(object)
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
        # Marshal daemon notifications from the background thread to the main thread.
        self._daemon_notification_received.connect(self._process_daemon_notification)
        self._rating_ready.connect(self._on_rating_ready)
        event_system.subscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)
        
        self._is_panning = False
        self._last_mouse_pos = QPoint()

        self.socket_client = None # Will be set by main window

    def set_socket_client(self, socket_client: ThumbnailSocketClient):
        self.socket_client = socket_client

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
                logging.debug(f"Published inspector event from picture view: {self._current_path} at {norm_pos.x():.2f}, {norm_pos.y():.2f}")
                    
        except Exception as e:  # why: called from mouse events; geometry errors must not crash the widget
            logging.error(f"Error updating inspector in picture view: {e}", exc_info=True)

    def loadImage(self, image_path: str, force_reload: bool = False) -> bool:
        """Load an image from the given path, preferring full resolution cached version."""
        # image_path here is always the ORIGINAL path
        if image_path == self._current_path and not force_reload:
            return True  # Already loaded, and not forced to reload
        
        if not self.socket_client:
            logging.error("Socket client not initialized in PictureView.")
            return False
        
        # Request the view image at FULLRES_REQUEST priority. If already cached the
        # response contains the path directly; otherwise generation is queued and we
        # wait for the previews_ready notification.
        path_to_load = None
        response = self.socket_client.request_view_image(image_path)
        if response and response.status == "success" and response.view_image_path:
            if os.path.exists(response.view_image_path):
                path_to_load = response.view_image_path

        if not path_to_load:
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

        success = self._picture_base.loadImageFromPath(path_to_load)

        if success:
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
            
            # Always start in fit mode for new images
            self._picture_base.setFitMode(True)
            
            # Reset drag zoom state when a new image is loaded
            self._picture_base.resetDragZoom()
            
            self.update()

            # Publish inspector event when the image changes
            # We simulate a mouse position in the center of the image (0.5, 0.5)
            # as there is no actual mouse movement during loading.
            event_data = InspectorEventData(
                event_type=EventType.INSPECTOR_UPDATE,
                source="picture_view",
                timestamp=time.time(),
                image_path=self._current_path, 
                normalized_position=QPointF(0.5, 0.5)
            )
            event_system.publish(event_data)
            logging.debug(f"Published inspector event from picture view on image load: {self._current_path} at 0.50, 0.50")

            return True
        else:
            logging.error(f"Failed to load image: {image_path}")
            return False
        
    @property
    def current_path(self) -> str:
        return self._current_path

    def _fetch_rating(self, path: str):
        """Fetch rating from daemon in a background thread and marshal result to main thread."""
        rating = 0
        if self.socket_client:
            try:
                resp = self.socket_client.get_metadata_batch([path])
                if resp and path in resp.metadata:
                    rating = resp.metadata[path].get("rating", 0) or 0
            except Exception as e:  # why: socket calls can raise ConnectionError/OSError/TimeoutError; emit zero so status bar gets a value
                logging.debug(f"Rating fetch failed for {path}: {e}")
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

    def _on_daemon_notification(self, event_data: DaemonNotificationEventData):
        """Thread bridge: called from the NotificationListener background thread."""
        self._daemon_notification_received.emit(event_data)

    @Slot(object)
    def _process_daemon_notification(self, event_data: DaemonNotificationEventData):
        if event_data.notification_type == "previews_ready":
            try:
                data = protocol.PreviewsReadyData.model_validate(event_data.data)

                # If this is the image we are waiting for, load it.
                if data.view_image_path and data.image_entry.path == self._current_path:
                    logging.info(f"Loading newly generated view image via notification: {data.image_entry.path}")
                    self.loadImage(data.image_entry.path, force_reload=True)
            except Exception as e:  # why: protocol errors must not crash the view
                logging.error(f"Error processing 'previews_ready' in PictureView: {e}", exc_info=True)

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

    def closeEvent(self, event):
        event_system.unsubscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)
        super().closeEvent(event)
