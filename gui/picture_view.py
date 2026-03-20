from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, Slot, QPointF, QSizeF, QPoint, QTimer
from PySide6.QtGui import QPainter, QImage, QMouseEvent, QPaintEvent, QResizeEvent, QKeyEvent

import logging
import math
logger = logging.getLogger(__name__)
import os
import time
import threading
from .picture_base import PictureBase, WHEEL_ZOOM_STEP
from core.event_system import event_system, EventType, InspectorEventData, StatusMessageEventData, StatusSection
from network.daemon_signals import DaemonSignals
from core.notifications import PreviewsReadyData

class PictureView(QWidget):

    escapePressed = Signal()
    zoomChanged = Signal(float)
    imageChanged = Signal(str)
    closeRequested = Signal()
    _rating_ready = Signal(str, int)  # (path, rating) — marshalled from bg thread

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self.config_manager = config_manager
        self._current_path = None
        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self.update)
        self._rating_ready.connect(self._on_rating_ready)
        self._daemon_signals: DaemonSignals | None = None

        self._is_panning = False
        self._interacting = False
        self._last_mouse_pos = QPoint()

        # Throttle inspector events to ~60 fps during continuous interaction.
        self._pending_inspector_pos = None
        self._inspector_timer = QTimer(self)
        self._inspector_timer.setSingleShot(True)
        self._inspector_timer.setInterval(16)  # ~60 fps
        self._inspector_timer.timeout.connect(self._flushInspectorEvent)

        # why: trackpad wheel events arrive in rapid bursts; treat the burst as
        # a single interaction so SmoothPixmapTransform stays off until idle.
        self._wheel_idle_timer = QTimer(self)
        self._wheel_idle_timer.setSingleShot(True)
        self._wheel_idle_timer.setInterval(150)
        self._wheel_idle_timer.timeout.connect(self._onWheelIdle)

        # why: named timer so closeEvent can stop it; anonymous singleShot
        # would fire on a deleted C++ widget if the user navigates away.
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(1000)
        self._retry_timer.timeout.connect(self._retry_load_current)

        self.service = None

    def set_service(self, service):
        self.service = service

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()

    def _queueInspectorUpdate(self, event_pos: QPointF) -> None:
        if not self._current_path or not self._picture_base.has_image():
            return
        self._pending_inspector_pos = event_pos
        if not self._inspector_timer.isActive():
            self._inspector_timer.start()

    def _flushInspectorEvent(self) -> None:
        pos = self._pending_inspector_pos
        if pos is None or not self._current_path or not self._picture_base.has_image():
            return
        self._pending_inspector_pos = None
        try:
            norm_pos = self._picture_base.screenToNormalized(pos)
            if 0 <= norm_pos.x() <= 1 and 0 <= norm_pos.y() <= 1:
                event_data = InspectorEventData(
                    event_type=EventType.INSPECTOR_UPDATE,
                    source="picture_view",
                    timestamp=time.time(),
                    image_path=self._current_path,
                    normalized_position=norm_pos
                )
                event_system.publish(event_data)
        except Exception as e:  # why: called from timer; geometry errors must not crash the widget
            logger.error(f"Error updating inspector in picture view: {e}", exc_info=True)

    def loadImage(self, image_path: str, force_reload: bool = False) -> bool:
        if image_path == self._current_path and not force_reload:
            return True

        if not self.service:
            logger.error("Service not initialized in PictureView.")
            return False

        _prev_fit = self._picture_base.isFitMode()
        _prev_center = QPointF(self._picture_base.viewState().center)
        _prev_fit_zoom = self._picture_base.calculateFitZoom()
        _prev_zoom = self._picture_base.viewState().zoom

        # why: three-way result — "memory" means bytes in daemon cache,
        # "view_image_path" means disk-cached, neither means generation queued.
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
            self._retry_timer.start()
            return False

        if result.get('view_image_source') == "memory":
            image_bytes = self.service.get_cached_view_image(image_path)
            success = self._picture_base.loadImageFromBytes(image_bytes) if image_bytes else False
        elif result.get('view_image_path'):
            success = self._picture_base.loadImageFromPath(result['view_image_path'])
        else:
            self._picture_base.setImage(QImage())
            self._current_path = image_path
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
            return False

        if success:
            # why: rotation is metadata-only — apply as a visual transform.
            self._apply_db_orientation(image_path)

            self._current_path = image_path
            self.imageChanged.emit(self._current_path)

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

    def _apply_db_orientation(self, image_path: str) -> None:
        if not self.service:
            return
        orientation = 1
        try:
            resp = self.service.get_metadata_batch([image_path])
            if resp and image_path in resp:
                orientation = resp[image_path].get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            pass
        self._picture_base.setOrientationFromExif(orientation)

    def rotate_current_image(self, degrees: int) -> None:
        if not self._picture_base.has_image():
            return
        self._picture_base.rotateBy(degrees)
        self._picture_base.setFitMode(True)
        self.update()

    @Slot(object)
    def _on_previews_ready(self, data: PreviewsReadyData) -> None:
        # If this is the image we are waiting for, load it.
        view_ready = data.view_image_path or data.view_image_source == "memory"
        if view_ready and data.image_entry.path == self._current_path:
            logger.info(f"Loading newly generated view image via notification: {data.image_entry.path}")
            self.loadImage(data.image_entry.path, force_reload=True)

    def _retry_load_current(self) -> None:
        if self._current_path:
            self.loadImage(self._current_path, force_reload=True)

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._picture_base.has_image():
            return

        painter = QPainter(self)
        # why: bilinear filtering is expensive at high zoom; skip during
        # active pan/drag-zoom for responsive interaction, re-enable on release.
        if not self._interacting:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)

        transform = self._picture_base.calculateTransform()
        painter.setTransform(transform)

        painter.drawImage(0, 0, self._picture_base.get_image())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._queueInspectorUpdate(QPointF(event.position()))

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
        self._queueInspectorUpdate(QPointF(event.position()))

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if self._interacting and not self._is_panning and not self._picture_base.isDragZooming():
            self._interacting = False
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._interacting = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

        elif event.button() == Qt.RightButton:
            self._interacting = True
            zoom_anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(zoom_anchor, QPointF(event.position()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self._interacting = False
            self.setCursor(Qt.ArrowCursor)
            self.update()  # final repaint with smooth transform

        elif event.button() == Qt.RightButton:
            self._interacting = False
            self._picture_base.endDragZoom()
            self.update()  # final repaint with smooth transform
            
                
    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
            
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                self._picture_base.setZoom(1.0, click_pos)
            else:
                self._picture_base.setFitMode(True)
            
            self.zoomChanged.emit(self._picture_base.viewState().zoom)
                
                
    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return

        self._interacting = True
        self._wheel_idle_timer.start()  # restart on each event

        # why: exponential scaling — trackpads send smaller deltas (~15-30)
        # so they produce proportionally finer adjustments vs mouse notches (120).
        log_zoom = math.log(self._picture_base.viewState().zoom)
        log_zoom += WHEEL_ZOOM_STEP * (delta / 120.0)
        new_zoom = math.exp(log_zoom)

        mouse_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
        self._picture_base.zoomAtAnchor(new_zoom, mouse_pos)

        self.zoomChanged.emit(self._picture_base.viewState().zoom)

    def _onWheelIdle(self) -> None:
        self._interacting = False
        self.update()

    def has_image(self) -> bool:
        return self._picture_base.has_image()

    def get_image(self):
        return self._picture_base.get_image()

    def screen_to_normalized(self, pos: QPointF) -> QPointF:
        return self._picture_base.screenToNormalized(pos)

    def zoom_in(self, factor: float = 1.25):
        self._picture_base.zoomIn(factor)

    def zoom_out(self, factor: float = 1.25):
        self._picture_base.zoomOut(factor)

    def closeEvent(self, event):
        self._inspector_timer.stop()
        self._wheel_idle_timer.stop()
        self._retry_timer.stop()
        if self._daemon_signals:
            self._daemon_signals.previews_ready.disconnect(self._on_previews_ready)
        super().closeEvent(event)
