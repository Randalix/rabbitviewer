# gui/inspector_view.py

import enum
import logging
from typing import Optional
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QPointF, QSize, QSettings, QPoint, Signal, QTimer
from PySide6.QtGui import QPainter, QImage
from .picture_base import PictureBase
from core.event_system import event_system, EventType, InspectorEventData, DaemonNotificationEventData
from network.socket_client import ThumbnailSocketClient
from network import protocol
from pydantic import ValidationError


class _ViewMode(enum.Enum):
    TRACKING = "tracking"
    FIT = "fit"
    MANUAL = "manual"


class InspectorView(QWidget):

    closed = Signal()

    def __init__(self, config_manager=None, inspector_index: int = 0):
        super().__init__(None, Qt.Window)
        self.config_manager = config_manager
        self._inspector_index = inspector_index
        self._picture_base = PictureBase()
        self._current_image_path = None
        self._view_image_ready = False
        self._is_panning = False
        self._last_mouse_pos = QPoint()

        self.setWindowTitle("Inspector")
        self.setMinimumSize(200, 200)

        settings = QSettings("RabbitViewer", "Inspector")
        geometry = settings.value(f"geometry_{self._inspector_index}")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(300, 300)

        # why: zoom_factor is a global preference (shared across inspector windows);
        # per-index QSettings keys (zoom_factor_0, zoom_factor_1, ...) are now orphaned.
        if self.config_manager:
            self._zoom_factor = float(self.config_manager.get("inspector.zoom_factor", 3.0))
        else:
            self._zoom_factor = 3.0
        try:
            view_mode_str = settings.value(f"view_mode_{self._inspector_index}", _ViewMode.TRACKING.value)
            self._view_mode = _ViewMode(view_mode_str)
        except ValueError:
            self._view_mode = _ViewMode.TRACKING
        self._update_window_title()

        self._picture_base.viewStateChanged.connect(self.update)

        self._picture_base.setZoom(self._zoom_factor)

        event_system.subscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)
        event_system.subscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)

        self.socket_client: Optional[ThumbnailSocketClient] = None

    def _on_daemon_notification(self, event_data: DaemonNotificationEventData):
        if event_data.notification_type == "previews_ready":
            try:
                data = protocol.PreviewsReadyData.model_validate(event_data.data)

                # If this is the image we are waiting for, load it directly.
                if data.view_image_path and data.image_path == self._current_image_path:
                    self._picture_base.loadImageFromPath(data.view_image_path)
                    self._view_image_ready = True
            # why: ValidationError covers malformed daemon payload; OSError covers
            # loadImageFromPath on a path deleted between previews_ready and load
            except (ValidationError, OSError) as e:
                logging.error(f"Error processing 'previews_ready' in InspectorView: {e}", exc_info=True)

    def _handle_inspector_update(self, event_data: InspectorEventData):
        if not self.isVisible():
            return

        logging.debug(f"Inspector received update event for: {event_data.image_path} at position ({event_data.normalized_position.x():.3f}, {event_data.normalized_position.y():.3f})")

        if not self.socket_client:
            logging.warning("InspectorView: Socket client not set, cannot get view image path.")
            return

        same_image = event_data.image_path == self._current_image_path

        # If the image changed, reset the ready flag.
        if not same_image:
            self._view_image_ready = False

        # Fast path: image is already loaded — skip the blocking socket call.
        # Known limitation: if the view image file is deleted from disk while the
        # inspector is open, this flag stays True and set_center() operates on stale
        # PictureBase data until the user moves to a different image.
        if same_image and self._view_image_ready:
            if self._view_mode == _ViewMode.TRACKING:
                self.set_center(event_data.normalized_position)
            return

        # why: daemon caches preview status in memory; this call is typically sub-millisecond
        response = self.socket_client.get_previews_status([event_data.image_path])
        view_image_path = None
        if response and response.status == "success":
            status = response.statuses.get(event_data.image_path)
            if status and status.view_image_ready and status.view_image_path:
                view_image_path = status.view_image_path

        if not view_image_path:
            # If image is not ready, request generation and clear view while waiting
            if not same_image:
                self._picture_base.setImage(QImage())
                self._current_image_path = event_data.image_path
            self.socket_client.request_previews([event_data.image_path])
            return

        if self._view_mode != _ViewMode.TRACKING:
            if not same_image:
                self.update_view(event_data.image_path, view_image_path, QPointF(0.5, 0.5))
        else:
            self.update_view(event_data.image_path, view_image_path, event_data.normalized_position)

    def update_view(self, original_image_path: str, view_image_path: str, norm_pos: QPointF):
        if not original_image_path:
            return

        if original_image_path != self._current_image_path:
            success = self._picture_base.loadImageFromPath(view_image_path)

            if success:
                self._current_image_path = original_image_path
                self._view_image_ready = True
                logging.info(f"Inspector displaying image: {original_image_path}")
                self._picture_base.setViewportSize(self.size())
                if self._view_mode == _ViewMode.FIT:
                    self._picture_base.setFitMode(True)
                else:
                    self._picture_base.setZoom(self._zoom_factor)
            else:
                self._current_image_path = None
                logging.warning(f"Inspector failed to load image: {original_image_path}")
                return

        if self._view_mode == _ViewMode.TRACKING:
            self.set_center(norm_pos)

    def set_center(self, norm_pos: QPointF):
        if self._current_image_path:
            self._picture_base.setCenter(norm_pos)
            self.update()

    def set_zoom_factor(self, zoom: float):
        self._zoom_factor = max(0.1, min(zoom, 20.0))
        self._update_window_title()
        # why: defer PictureBase sync until an image is loaded; update_view() applies
        # self._zoom_factor when the next image loads, keeping state consistent.
        if self._current_image_path:
            if self._view_mode == _ViewMode.FIT:
                self._view_mode = _ViewMode.TRACKING
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)

    def _enter_manual_mode(self):
        """Detach from thumbnail mouse tracking; user has taken direct control."""
        if self._view_mode != _ViewMode.MANUAL:
            self._view_mode = _ViewMode.MANUAL
            self._update_window_title()

    def _update_window_title(self):
        if self._view_mode == _ViewMode.FIT:
            self.setWindowTitle("Inspector - Fit Mode")
        elif self._view_mode == _ViewMode.MANUAL:
            self.setWindowTitle(f"Inspector - Locked ({self._zoom_factor:.1f}x Zoom)")
        else:
            self.setWindowTitle(f"Inspector - Tracking ({self._zoom_factor:.1f}x Zoom)")

    def paintEvent(self, event):
        if not self._current_image_path or not self._picture_base.has_image():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), Qt.black)
        current_size = self.size()
        if self._picture_base.viewportSize() != current_size:
            self._picture_base.setViewportSize(current_size)
        transform = self._picture_base.calculateTransform()
        painter.setTransform(transform)
        image_rect = self._picture_base.imageRect()
        painter.drawImage(image_rect, self._picture_base.get_image())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._current_image_path:
            self._picture_base.setViewportSize(self.size())
            if self._view_mode == _ViewMode.FIT:
                self._picture_base.setFitMode(True)

    def mousePressEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
            return
        self._enter_manual_mode()
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
            zoom_anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(zoom_anchor, QPointF(event.position()))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
            return
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
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
            if self._view_mode == _ViewMode.FIT:
                # Go to 100% zoom in auto-tracking mode
                self._zoom_factor = 1.0
                self._view_mode = _ViewMode.TRACKING
                self._picture_base.setFitMode(False)
                self._picture_base.setZoom(1.0)
            else:
                # Go to fit mode
                self._view_mode = _ViewMode.FIT
                self._picture_base.setFitMode(True)
            self._update_window_title()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)
        self._enter_manual_mode()
        factor = 1.25 if event.angleDelta().y() > 0 else 1/1.25
        self.set_zoom_factor(self._zoom_factor * factor)
        event.accept()

    def set_socket_client(self, socket_client: ThumbnailSocketClient):
        self.socket_client = socket_client

    def closeEvent(self, event):
        settings = QSettings("RabbitViewer", "Inspector")
        settings.setValue(f"geometry_{self._inspector_index}", self.saveGeometry())
        settings.setValue(f"view_mode_{self._inspector_index}", self._view_mode.value)
        settings.sync()
        if self.config_manager:
            self.config_manager.set("inspector.zoom_factor", self._zoom_factor)
        event_system.unsubscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)
        event_system.unsubscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()
