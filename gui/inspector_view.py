# gui/inspector_view.py

import enum
import logging
import os
import threading
from typing import Optional
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QPointF, QSettings, QPoint, Signal, Slot
from PySide6.QtGui import QPainter, QImage
from .picture_base import PictureBase
from core.event_system import event_system, EventType, InspectorEventData, DaemonNotificationEventData
from network.socket_client import ThumbnailSocketClient
from network import protocol
from plugins.video_plugin import VIDEO_EXTENSIONS

_VIDEO_EXTENSIONS = frozenset(VIDEO_EXTENSIONS)


def _is_video(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lower() in _VIDEO_EXTENSIONS


class _ViewMode(enum.Enum):
    TRACKING = "tracking"
    FIT = "fit"
    MANUAL = "manual"


class InspectorView(QWidget):

    closed = Signal()
    # Delivers background-thread socket results back to the GUI thread.
    # Args: (image_path, view_image_path_or_empty, normalized_position)
    _preview_status_ready = Signal(str, str, QPointF)
    # Bridges DAEMON_NOTIFICATION from the NotificationClient thread to the GUI thread.
    _daemon_notification_received = Signal(object)
    # Delivers a video frame grabbed on a background thread to the GUI thread.
    _video_frame_ready = Signal(QImage)

    def __init__(self, config_manager=None, inspector_index: int = 0):
        super().__init__(None, Qt.Window)
        self.config_manager = config_manager
        self._inspector_index = inspector_index
        self._picture_base = PictureBase()
        self._current_image_path = None
        self._view_image_ready = False
        self._is_panning = False
        self._last_mouse_pos = QPoint()

        # Background-fetch tracking: only one socket fetch in flight at a time.
        self._desired_image_path: Optional[str] = None
        self._desired_norm_pos: QPointF = QPointF(0.5, 0.5)
        self._fetch_in_flight: Optional[str] = None
        # why: CPython GIL makes single bool read/write atomic; the worst case is
        # one extra signal emission after closeEvent, which Qt discards safely.
        self._fetch_cancelled = False

        # Video scrub state
        self._scrub_player = None       # headless mpv for frame extraction
        self._scrub_video_path: str | None = None
        self._scrub_duration: float = 0.0
        self._is_video_mode: bool = False
        # Persistent scrub worker: only one thread, processes latest request.
        self._scrub_request: Optional[tuple] = None  # (video_path, norm_x)
        self._scrub_lock = threading.Lock()
        self._scrub_event = threading.Event()
        self._scrub_thread: Optional[threading.Thread] = None
        self._scrub_stop = False

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
        self._preview_status_ready.connect(self._on_preview_status_ready)
        self._daemon_notification_received.connect(self._on_daemon_notification)
        self._video_frame_ready.connect(self._on_video_frame_ready)

        self._picture_base.setZoom(self._zoom_factor)

        event_system.subscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)
        event_system.subscribe(EventType.DAEMON_NOTIFICATION, self._daemon_notification_from_thread)

        self.socket_client: Optional[ThumbnailSocketClient] = None

    def _daemon_notification_from_thread(self, event_data: DaemonNotificationEventData):
        # why: DAEMON_NOTIFICATION callbacks fire on the NotificationClient thread;
        # emit a signal so _on_daemon_notification runs on the GUI thread.
        self._daemon_notification_received.emit(event_data)

    @Slot(object)
    def _on_daemon_notification(self, event_data: DaemonNotificationEventData):
        if event_data.notification_type == "previews_ready":
            try:
                data = protocol.PreviewsReadyData.model_validate(event_data.data)

                # Accept previews for the image we already display OR the image
                # the user currently wants (slow-drive case: the fetch returned
                # empty and _current_image_path was never set to the desired path).
                # Skip if in video mode — scrub worker manages the display.
                target = data.image_entry.path
                is_target = (target == self._current_image_path
                             or target == self._desired_image_path)
                if data.view_image_path and is_target and not self._is_video_mode:
                    norm_pos = self._desired_norm_pos if self._view_mode == _ViewMode.TRACKING else QPointF(0.5, 0.5)
                    self.update_view(target, data.view_image_path, norm_pos)
            # why: ValidationError covers malformed daemon payload; OSError covers
            # loadImageFromPath on a path deleted between previews_ready and load
            except (ValueError, TypeError, KeyError, OSError) as e:
                logging.error("Error processing 'previews_ready' in InspectorView: %s", e, exc_info=True)

    def _handle_inspector_update(self, event_data: InspectorEventData):
        if not self.isVisible():
            return

        logging.debug("Inspector update: %s at (%.3f, %.3f)",
                      event_data.image_path,
                      event_data.normalized_position.x(),
                      event_data.normalized_position.y())

        image_path = event_data.image_path
        norm_pos = event_data.normalized_position

        # ------ VIDEO PATH ------
        if _is_video(image_path):
            self._is_video_mode = True
            self._desired_image_path = image_path
            self._desired_norm_pos = norm_pos
            self._current_image_path = image_path
            self._view_image_ready = True

            if self._view_mode in (_ViewMode.TRACKING, _ViewMode.FIT):
                self._request_video_frame(image_path, norm_pos.x())
            # MANUAL: user controls scrub via mouse drag in inspector
            self._update_window_title()
            return

        # ------ IMAGE PATH (existing logic) ------
        if self._is_video_mode:
            self._is_video_mode = False
            self._update_window_title()

        self._desired_image_path = image_path
        self._desired_norm_pos = norm_pos

        same_image = image_path == self._current_image_path
        if not same_image:
            self._view_image_ready = False

        # why: skip socket if image already loaded; _view_image_ready stays True
        # until a different image is hovered (stale only if view file is deleted).
        if same_image and self._view_image_ready:
            if self._view_mode == _ViewMode.TRACKING:
                self.set_center(norm_pos)
            return

        if not self.socket_client:
            return

        # If a fetch is already in flight for this exact path, skip — the result
        # will arrive via _on_preview_status_ready when the thread finishes.
        if self._fetch_in_flight == image_path:
            return

        # Clear the display immediately when switching to a new image.
        if not same_image and self._current_image_path is not None:
            self._picture_base.setImage(QImage())

        self._fetch_in_flight = image_path
        threading.Thread(
            target=self._fetch_preview_status,
            args=(image_path, norm_pos),
            daemon=True,
            name="inspector-fetch",
        ).start()

    def _fetch_preview_status(self, image_path: str, norm_pos: QPointF):
        try:
            response = self.socket_client.get_previews_status([image_path])
            view_image_path = ""
            if response and response.status == "success":
                status = response.statuses.get(image_path)
                if status and status.view_image_ready and status.view_image_path:
                    view_image_path = status.view_image_path

            if not view_image_path:
                # Request generation; result will arrive via previews_ready daemon notification.
                self.socket_client.request_view_image(image_path)
        except Exception as e:
            # why: socket calls can raise ConnectionError/OSError/TimeoutError on
            # NAS drop or pool exhaustion; log and emit empty path to unblock GUI.
            logging.error("Inspector: error fetching preview status for %s: %s", image_path, e)
            view_image_path = ""

        if not self._fetch_cancelled:
            self._preview_status_ready.emit(image_path, view_image_path, norm_pos)

    @Slot(str, str, QPointF)
    def _on_preview_status_ready(self, image_path: str, view_image_path: str, norm_pos: QPointF):
        if self._fetch_in_flight == image_path:
            self._fetch_in_flight = None

        # Discard stale results if the user has already moved to a different image.
        if image_path != self._desired_image_path:
            return

        if not view_image_path:
            # Not ready yet; request was already sent in the background thread.
            return

        if self._view_mode != _ViewMode.TRACKING:
            if image_path != self._current_image_path:
                self.update_view(image_path, view_image_path, QPointF(0.5, 0.5))
        else:
            self.update_view(image_path, view_image_path, norm_pos)

    def update_view(self, original_image_path: str, view_image_path: str, norm_pos: QPointF):
        if not original_image_path:
            return

        if original_image_path != self._current_image_path:
            success = self._picture_base.loadImageFromPath(view_image_path)

            if success:
                self._current_image_path = original_image_path
                self._view_image_ready = True
                logging.info("Inspector displaying image: %s", original_image_path)
                self._picture_base.setViewportSize(self.size())
                if self._view_mode == _ViewMode.FIT:
                    self._picture_base.setFitMode(True)
                else:
                    self._picture_base.setZoom(self._zoom_factor)
            else:
                self._current_image_path = None
                logging.warning("Inspector failed to load image: %s", original_image_path)
                return

        if self._view_mode == _ViewMode.TRACKING:
            self.set_center(norm_pos)

    def set_center(self, norm_pos: QPointF):
        if self._current_image_path:
            # why: setCenter emits viewStateChanged → self.update; explicit update() is redundant.
            self._picture_base.setCenter(norm_pos)

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
        if self._is_video_mode:
            if self._view_mode == _ViewMode.FIT:
                self.setWindowTitle("Inspector - Video Fit")
            elif self._view_mode == _ViewMode.MANUAL:
                self.setWindowTitle("Inspector - Video Scrub (Manual)")
            else:
                self.setWindowTitle("Inspector - Video Scrub (Tracking)")
        else:
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
        # why: viewport size is kept current by resizeEvent; no need to sync here
        # (doing so inside paintEvent emits viewStateChanged → schedules a second repaint)
        transform = self._picture_base.calculateTransform()
        painter.setTransform(transform)
        image_rect = self._picture_base.imageRect()
        painter.drawImage(image_rect, self._picture_base.get_image())

    def showEvent(self, event):
        super().showEvent(event)
        # why: _fetch_cancelled is set True in closeEvent; reset here so fetches
        # work again when the window is re-opened in the same session.
        self._fetch_cancelled = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._current_image_path:
            self._picture_base.setViewportSize(self.size())
            if self._view_mode == _ViewMode.FIT:
                self._picture_base.setFitMode(True)

    def mousePressEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
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

        # Video manual scrub: mouse X maps to timeline position.
        if self._is_video_mode and self._view_mode == _ViewMode.MANUAL:
            if self._is_panning and self.width() > 0 and self._current_image_path:
                norm_x = max(0.0, min(1.0, event.position().x() / self.width()))
                self._request_video_frame(self._current_image_path, norm_x)
            return

        if self._is_panning:
            self._enter_manual_mode()
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
                self._view_mode = _ViewMode.TRACKING
                self._picture_base.setFitMode(False)
                self._picture_base.setZoom(self._zoom_factor)
            else:
                self._view_mode = _ViewMode.FIT
                self._picture_base.setFitMode(True)
            self._update_window_title()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        if self._view_mode == _ViewMode.FIT:
            self._view_mode = _ViewMode.TRACKING
            self._picture_base.setFitMode(False)
            self._picture_base.setZoom(self._zoom_factor)
            self._update_window_title()
        factor = 1.25 if event.angleDelta().y() > 0 else 1/1.25
        self.set_zoom_factor(self._zoom_factor * factor)
        event.accept()

    def set_socket_client(self, socket_client: ThumbnailSocketClient):
        self.socket_client = socket_client

    # --------------------------------------------------------- video scrub player

    def _request_video_frame(self, video_path: str, norm_x: float):
        """Post the latest scrub request; the persistent worker picks it up."""
        with self._scrub_lock:
            self._scrub_request = (video_path, norm_x)
        self._scrub_event.set()
        # Start the worker thread on first request.
        if self._scrub_thread is None or not self._scrub_thread.is_alive():
            self._scrub_stop = False
            self._scrub_thread = threading.Thread(
                target=self._scrub_worker,
                daemon=True,
                name="inspector-scrub-worker",
            )
            self._scrub_thread.start()

    def _scrub_worker(self):
        """Persistent background thread: processes the latest scrub request."""
        try:
            import mpv as _mpv
        except Exception as e:
            logging.error("Failed to import mpv for scrub worker: %s", e)
            return

        player = None
        loaded_path: str | None = None
        duration: float = 0.0

        try:
            player = _mpv.MPV(vo="null", ao="null", aid="no", hwdec="auto-safe",
                               hr_seek="yes", keep_open="yes",
                               pause=True)
        except Exception as e:
            logging.error("Failed to create scrub player: %s", e)
            return

        import time as _time

        while not self._scrub_stop:
            self._scrub_event.wait(timeout=5.0)
            if self._scrub_stop:
                break
            self._scrub_event.clear()

            # Drain to latest request.
            with self._scrub_lock:
                req = self._scrub_request
                self._scrub_request = None
            if req is None:
                continue

            video_path, norm_x = req

            try:
                # Load video if changed.
                if loaded_path != video_path:
                    player.play(video_path)
                    # Player starts paused; poll until demuxer reports duration.
                    for _ in range(100):
                        _time.sleep(0.02)
                        dur = player.duration
                        if dur and dur > 0:
                            break
                    duration = player.duration or 0.0
                    loaded_path = video_path

                if duration <= 0:
                    continue

                target = max(0.0, min(norm_x * duration, duration))
                player.seek(target, reference="absolute")
                # Wait for the async seek to settle before grabbing.
                for _ in range(25):
                    _time.sleep(0.01)
                    tp = player.time_pos
                    if tp is not None and abs(tp - target) < 0.1:
                        break

                raw = player.screenshot_raw()
                if hasattr(raw, 'tobytes'):
                    if raw.mode != 'RGBA':
                        raw = raw.convert('RGBA')
                    w, h = raw.size
                    data = raw.tobytes()
                    qimg = QImage(data, w, h, QImage.Format_RGBA8888).copy()
                    if not self._scrub_stop:
                        self._video_frame_ready.emit(qimg)
            except Exception as e:
                logging.debug("Scrub worker frame grab failed: %s", e)

        # Cleanup.
        try:
            player.terminate()
        except Exception:
            pass

    @Slot(QImage)
    def _on_video_frame_ready(self, frame: QImage):
        """Receive a grabbed frame on the GUI thread and display it."""
        if not self._is_video_mode:
            return
        if frame and not frame.isNull():
            self._picture_base.setImage(frame)
            self._picture_base.setViewportSize(self.size())
            self._picture_base.setFitMode(True)

    def _destroy_scrub_player(self):
        self._scrub_stop = True
        self._scrub_event.set()
        if self._scrub_thread and self._scrub_thread.is_alive():
            self._scrub_thread.join(timeout=2)
        self._scrub_thread = None
        self._scrub_video_path = None
        self._scrub_duration = 0.0

    def closeEvent(self, event):
        # Signal any in-flight background fetch to discard its result.
        self._fetch_cancelled = True
        self._destroy_scrub_player()
        settings = QSettings("RabbitViewer", "Inspector")
        settings.setValue(f"geometry_{self._inspector_index}", self.saveGeometry())
        settings.setValue(f"view_mode_{self._inspector_index}", self._view_mode.value)
        settings.sync()
        if self.config_manager:
            self.config_manager.set("inspector.zoom_factor", self._zoom_factor)
        event_system.unsubscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)
        event_system.unsubscribe(EventType.DAEMON_NOTIFICATION, self._daemon_notification_from_thread)
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()
