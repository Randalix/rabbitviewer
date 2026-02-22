import logging
import threading
import time
import os

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPointF, QRectF
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter, QImage

from core.event_system import (
    event_system, EventType, InspectorEventData,
    StatusMessageEventData, StatusSection,
)
from network.socket_client import ThumbnailSocketClient

logger = logging.getLogger(__name__)


class VideoView(QWidget):
    escapePressed = Signal()
    closeRequested = Signal()
    _frame_ready = Signal(QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self._current_path: str | None = None
        self._duration: float = 0.0
        self.socket_client: ThumbnailSocketClient | None = None

        self._player = None
        self._frame: QImage | None = None

        # Background frame-grab thread
        self._grab_thread: threading.Thread | None = None
        self._grab_stop = threading.Event()

        self._frame_ready.connect(self._on_frame_ready)

        # Status timer: updates the status bar with playback info.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._update_status)

    def set_socket_client(self, client: ThumbnailSocketClient):
        self.socket_client = client

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def duration(self) -> float:
        return self._duration

    def loadVideo(self, path: str) -> bool:
        """Load and start playing a video file."""
        if path == self._current_path and self._player:
            return True

        self._destroy_player()
        self._current_path = path
        self._duration = 0.0
        self._frame = None

        try:
            import mpv
            self._player = mpv.MPV(
                vo="null",
                ao="null",
                input_default_bindings=False,
                input_vo_keyboard=False,
                mute=True,
                keep_open="yes",
                loop_file="inf",
                hwdec="auto-safe",
            )
            self._player.play(path)

            # Start background frame grabber.
            self._grab_stop.clear()
            self._grab_thread = threading.Thread(
                target=self._frame_grab_loop,
                daemon=True,
                name="video-frame-grab",
            )
            self._grab_thread.start()
            self._status_timer.start()

            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="video_view",
                timestamp=time.time(),
                message=path,
                section=StatusSection.FILEPATH,
            ))
            return True
        except Exception as e:
            logger.error("Failed to create mpv player: %s", e, exc_info=True)
            self._current_path = None
            return False

    # ----------------------------------------------------------- frame grabber

    def _frame_grab_loop(self):
        """Background thread: grab frames from mpv and emit signal to GUI."""
        while not self._grab_stop.is_set():
            player = self._player
            if not player:
                break
            try:
                raw = player.screenshot_raw()
                if hasattr(raw, 'tobytes'):
                    if raw.mode != 'RGBA':
                        raw = raw.convert('RGBA')
                    w, h = raw.size
                    data = raw.tobytes()
                    qimg = QImage(data, w, h, QImage.Format_RGBA8888).copy()
                    if not self._grab_stop.is_set():
                        self._frame_ready.emit(qimg)
            except Exception:
                pass
            self._grab_stop.wait(0.033)  # ~30 fps

    @Slot(QImage)
    def _on_frame_ready(self, frame: QImage):
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        if not self._frame or self._frame.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), Qt.black)

        # Fit frame within widget while preserving aspect ratio.
        fw, fh = self._frame.width(), self._frame.height()
        ww, wh = self.width(), self.height()
        scale = min(ww / fw, wh / fh)
        dw, dh = fw * scale, fh * scale
        x = (ww - dw) / 2
        y = (wh - dh) / 2
        painter.drawImage(QRectF(x, y, dw, dh), self._frame)
        painter.end()

    # ------------------------------------------------------------------ input

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key_Escape:
            self.escapePressed.emit()
        elif key == Qt.Key_Space:
            self._toggle_pause()
        elif key == Qt.Key_M:
            self._toggle_mute()
        elif key == Qt.Key_BracketRight:
            self._seek(5)
        elif key == Qt.Key_BracketLeft:
            self._seek(-5)
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        """Publish inspector update: normalized X = timeline position."""
        if not self._current_path:
            return
        w = self.width()
        if w <= 0:
            return
        norm_x = max(0.0, min(1.0, event.position().x() / w))
        event_system.publish(InspectorEventData(
            event_type=EventType.INSPECTOR_UPDATE,
            source="video_view",
            timestamp=time.time(),
            image_path=self._current_path,
            normalized_position=QPointF(norm_x, 0.5),
        ))

    # ------------------------------------------------------- playback control

    def _toggle_pause(self):
        if self._player:
            self._player.pause = not self._player.pause

    def _toggle_mute(self):
        if self._player:
            self._player.mute = not self._player.mute

    def _seek(self, seconds: float):
        if self._player:
            self._player.seek(seconds, reference="relative")

    # --------------------------------------------------------------- status bar

    @Slot()
    def _update_status(self):
        """Poll mpv for time-pos and duration, publish to status bar."""
        if not self._player:
            return
        try:
            pos = self._player.time_pos or 0.0
            dur = self._player.duration or 0.0
            self._duration = dur
            paused = self._player.pause
            muted = self._player.mute

            parts = []
            if paused:
                parts.append("PAUSED")
            if muted:
                parts.append("MUTED")
            status = " | ".join(parts) if parts else "PLAYING"

            msg = f"{self._fmt(pos)} / {self._fmt(dur)}  [{status}]"
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="video_view",
                timestamp=time.time(),
                message=msg,
                section=StatusSection.PROCESS,
                timeout=0,
            ))
        except Exception:
            pass  # mpv may not be ready yet

    @staticmethod
    def _fmt(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # ---------------------------------------------------------------- lifecycle

    def _destroy_player(self):
        self._grab_stop.set()
        self._status_timer.stop()
        if self._grab_thread:
            self._grab_thread.join(timeout=2)
            self._grab_thread = None
        if self._player:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        self._frame = None

    def closeEvent(self, event):
        self._destroy_player()
        super().closeEvent(event)
