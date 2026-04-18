import logging
import time
import subprocess

from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent, QOpenGLContext, QPainter, QPixmap

from core.event_system import (
    event_system, EventType, InspectorEventData,
    StatusMessageEventData, StatusSection,
)

logger = logging.getLogger(__name__)


def _get_proc_address(_ctx, name):
    ctx = QOpenGLContext.currentContext()
    if ctx is None:
        return 0
    addr = ctx.getProcAddress(name)
    return addr


class VideoView(QOpenGLWidget):
    escapePressed = Signal()
    navigateRequested = Signal(str)  # "next" or "previous"
    _mpv_frame_ready = Signal()  # thread-safe bridge from mpv decode thread → Qt
    firstFrameReady = Signal()   # emitted once per loadVideo when first frame arrives
    positionChanged = Signal(float)
    playbackStateChanged = Signal(bool)

    def __init__(self, scrub: bool = False, parent=None):
        super().__init__(parent)
        self._scrub = scrub
        if not scrub:
            self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self._current_path: str | None = None
        self._duration: float = 0.0
        self.service = None

        self._player = None
        self._render_ctx = None
        self._has_first_frame: bool = False
        self._fallback_pixmap: QPixmap | None = None

        self._mpv_frame_ready.connect(self._on_qt_frame_ready, Qt.QueuedConnection)

        if not scrub:
            self._status_timer = QTimer(self)
            self._status_timer.setInterval(250)
            self._status_timer.timeout.connect(self._update_status)

    def set_service(self, service):
        self.service = service

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def duration(self) -> float:
        return self._duration

    def initializeGL(self):
        logger.debug("[flash] initializeGL scrub=%s size=(%d,%d)", self._scrub, self.width(), self.height())

    def showEvent(self, event):
        logger.debug("[flash] showEvent scrub=%s size=(%d,%d) first_frame=%s fallback=%s",
                     self._scrub, self.width(), self.height(),
                     self._has_first_frame, self._fallback_pixmap is not None)
        super().showEvent(event)

    def resizeEvent(self, event):
        logger.debug("[flash] resizeEvent scrub=%s old=(%d,%d) new=(%d,%d) first_frame=%s fallback=%s",
                     self._scrub,
                     event.oldSize().width(), event.oldSize().height(),
                     event.size().width(), event.size().height(),
                     self._has_first_frame, self._fallback_pixmap is not None)
        super().resizeEvent(event)

    def loadVideo(self, path: str, start_pct: float = 0.0) -> bool:
        if path == self._current_path and self._player:
            logger.debug("[flash] loadVideo early-return (same path) size=(%d,%d)", self.width(), self.height())
            return True

        self._destroy_player()
        self._current_path = path
        self._duration = 0.0
        self._has_first_frame = False
        logger.debug("[flash] loadVideo reset first_frame=False path=%s", path)

        # Geometry is already set by the caller (_start_video_playback).
        # Do not override it here.

        try:
            import mpv

            if self._scrub:
                mpv_kwargs = dict(
                    vo="libmpv",
                    input_default_bindings=False,
                    input_vo_keyboard=False,
                    keep_open="yes",
                    loop_file="inf",
                    hwdec="auto-safe",
                    hr_seek="yes",
                    pause=True,
                    aid="no",
                    # Widget is sized to the JPEG thumbnail's displayed rect
                    # (via _pixmap_rect_in_label), whose aspect matches the
                    # decoded video's. keepaspect=yes makes mpv letterbox
                    # inside that rect the same way ffmpeg's scale filter did
                    # — so the first hover frame aligns pixel-for-pixel with
                    # the thumbnail instead of shifting by 1-2 px from the
                    # stretched-fill rounding.
                    keepaspect="yes",
                )
                if start_pct > 0.0:
                    mpv_kwargs["start"] = f"{start_pct * 100:.1f}%"
                self._player = mpv.MPV(**mpv_kwargs)
            else:
                self._player = mpv.MPV(
                    vo="libmpv",
                    input_default_bindings=True,
                    input_vo_keyboard=True,
                    keep_open="yes",
                    loop_file="inf",
                    hwdec="auto-safe",
                    osc=True,
                )
                # Unbind keys we override so mpv doesn't also handle them.
                self._player.command('keybind', 'q', 'ignore')
                self._player.command('keybind', 'Q', 'ignore')
                self._player.command('keybind', 'a', 'ignore')
                self._player.command('keybind', 'd', 'ignore')

            self.makeCurrent()
            from mpv import MpvRenderContext, MpvGlGetProcAddressFn
            proc_addr_fn = MpvGlGetProcAddressFn(_get_proc_address)
            # prevent garbage collection of the ctypes callback
            self._proc_addr_fn = proc_addr_fn
            self._render_ctx = MpvRenderContext(
                self._player, 'opengl',
                opengl_init_params={'get_proc_address': proc_addr_fn},
            )
            self._render_ctx.update_cb = self._on_mpv_update
            self.doneCurrent()

            self._player.play(path)
            self.playbackStateChanged.emit(True)  # Emit signal when playback starts

            if not self._scrub:
                self._status_timer.start()
                event_system.publish(StatusMessageEventData(
                    event_type=EventType.STATUS_MESSAGE,
                    source="video_view",
                    timestamp=time.time(),
                    message=path,
                    section=StatusSection.FILEPATH,
                ))
            return True
        except Exception as e:  # why: MpvRenderContext init propagates undocumented C-level exceptions on driver/GL failure
            logger.error("Failed to create mpv player: %s", e, exc_info=True)
            self._current_path = None
            return False

    def _seek_to_seconds(self, seconds: float):
        """Seek to an absolute time. Uses exact precision for short clips (<10s),
        keyframes for longer ones so scrubbing stays responsive."""
        if not self._player:
            return
        try:
            if not self._duration:
                dur = self._player.duration
                if not dur or dur <= 0:
                    return
                self._duration = dur
            target = max(0.0, min(seconds, self._duration))
            precision = "exact" if self._duration < 10.0 else "keyframes"
            self._player.seek(target, reference="absolute", precision=precision)
        except Exception:  # why: mpv property/command raises on terminated player
            pass

    def seek_normalized(self, norm_x: float):
        if not self._duration:
            try:
                dur = self._player.duration if self._player else None
                if dur and dur > 0:
                    self._duration = dur
            except Exception:
                pass
        self._seek_to_seconds(norm_x * self._duration)

    def setPosition(self, position: float):
        """Seek to a normalised position (0–1). Used by the thumbnail scrub overlay."""
        if not self._duration:
            try:
                dur = self._player.duration if self._player else None
                if dur and dur > 0:
                    self._duration = dur
            except Exception:
                pass
        self._seek_to_seconds(position * self._duration)
        self.playbackStateChanged.emit(False)

    # --------------------------------------------------------- mpv render callback

    def _on_mpv_update(self):
        self._mpv_frame_ready.emit()

    def set_fallback_pixmap(self, pixmap: QPixmap | None):
        self._fallback_pixmap = pixmap

    @Slot()
    def _on_qt_frame_ready(self):
        if not self._has_first_frame:
            # Gate on pts being non-None: mpv fires its update callback before
            # the first frame is decoded, so _has_first_frame must not be set
            # until there is actually something to render (pts != None).
            try:
                pts_ready = self._player.time_pos is not None if self._player else False
            except Exception:
                pts_ready = False
            if pts_ready:
                self._has_first_frame = True
                self._fallback_pixmap = None
                logger.debug("[flash] first real frame ready size=(%d,%d)", self.width(), self.height())
                self.firstFrameReady.emit()
        self.update()

    def paintGL(self):
        if self._scrub and not self._has_first_frame and self._fallback_pixmap and not self._fallback_pixmap.isNull():
            logger.debug("[flash] paintGL FALLBACK size=(%d,%d)", self.width(), self.height())
            scaled = self._fallback_pixmap.scaled(
                self.width(), self.height(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            painter = QPainter(self)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.end()
            return
        if not self._render_ctx:
            return
        fbo = self.defaultFramebufferObject()
        dpr = self.devicePixelRatio()
        w = int(self.width() * dpr)
        h = int(self.height() * dpr)
        if self._scrub:
            logger.debug(
                "[align-mpv] paintGL css=(%d,%d) dpr=%.2f fbo=(%d,%d) widget_pos=(%d,%d)",
                self.width(), self.height(), dpr, w, h, self.x(), self.y(),
            )
        self._render_ctx.render(
            opengl_fbo={'w': w, 'h': h, 'fbo': fbo},
            flip_y=True,
        )
        self._render_ctx.report_swap()

    # ------------------------------------------------------------------ input

    _QT_TO_MPV = {
        Qt.Key_Space: 'SPACE', Qt.Key_Return: 'ENTER', Qt.Key_Enter: 'ENTER',
        Qt.Key_Tab: 'TAB', Qt.Key_Backspace: 'BS', Qt.Key_Delete: 'DEL',
        Qt.Key_Insert: 'INS', Qt.Key_Home: 'HOME', Qt.Key_End: 'END',
        Qt.Key_PageUp: 'PGUP', Qt.Key_PageDown: 'PGDWN',
        Qt.Key_Left: 'LEFT', Qt.Key_Right: 'RIGHT',
        Qt.Key_Up: 'UP', Qt.Key_Down: 'DOWN',
        Qt.Key_Escape: 'ESC',
        Qt.Key_F1: 'F1', Qt.Key_F2: 'F2', Qt.Key_F3: 'F3', Qt.Key_F4: 'F4',
        Qt.Key_F5: 'F5', Qt.Key_F6: 'F6', Qt.Key_F7: 'F7', Qt.Key_F8: 'F8',
        Qt.Key_F9: 'F9', Qt.Key_F10: 'F10', Qt.Key_F11: 'F11', Qt.Key_F12: 'F12',
    }

    def keyPressEvent(self, event: QKeyEvent):
        if self._scrub:
            super().keyPressEvent(event)
            return

        key = event.key()

        if key == Qt.Key_Q:
            self.escapePressed.emit()
            return
        if key == Qt.Key_A:
            self.navigateRequested.emit("previous")
            return
        if key == Qt.Key_D:
            self.navigateRequested.emit("next")
            return

        if self._player:
            mpv_key = self._qt_key_to_mpv(event)
            if mpv_key:
                try:
                    self._player.command('keypress', mpv_key)
                except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
                    pass
                return

        super().keyPressEvent(event)

    def _qt_key_to_mpv(self, event: QKeyEvent) -> str | None:
        key = event.key()
        text = event.text()
        mods = event.modifiers()

        name = self._QT_TO_MPV.get(key)
        if name is None:
            if text and text.isprintable():
                name = text
            else:
                return None

        # mpv format: Shift+Ctrl+Alt+Meta+KEY
        prefixes = []
        if mods & Qt.ShiftModifier and key not in (Qt.Key_Shift,):
            prefixes.append('Shift')
        if mods & Qt.ControlModifier:
            prefixes.append('Ctrl')
        if mods & Qt.AltModifier:
            prefixes.append('Alt')
        if mods & Qt.MetaModifier:
            prefixes.append('Meta')

        if prefixes:
            return '+'.join(prefixes) + '+' + name
        return name

    def _send_mouse_pos(self, event: QMouseEvent):
        dpr = self.devicePixelRatio()
        x = int(event.position().x() * dpr)
        y = int(event.position().y() * dpr)
        try:
            self._player.command('mouse', x, y)
        except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
            pass

    def mousePressEvent(self, event: QMouseEvent):
        if self._scrub or not self._player:
            super().mousePressEvent(event)
            return
        self._send_mouse_pos(event)
        btn = {Qt.LeftButton: 'MBTN_LEFT', Qt.RightButton: 'MBTN_RIGHT',
               Qt.MiddleButton: 'MBTN_MID'}.get(event.button())
        if btn:
            try:
                self._player.command('keydown', btn)
            except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
                pass

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._scrub or not self._player:
            super().mouseReleaseEvent(event)
            return
        self._send_mouse_pos(event)
        btn = {Qt.LeftButton: 'MBTN_LEFT', Qt.RightButton: 'MBTN_RIGHT',
               Qt.MiddleButton: 'MBTN_MID'}.get(event.button())
        if btn:
            try:
                self._player.command('keyup', btn)
            except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
                pass

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self._scrub or not self._player:
            super().mouseDoubleClickEvent(event)
            return
        self._send_mouse_pos(event)
        if event.button() == Qt.LeftButton:
            try:
                self._player.command('keypress', 'MBTN_LEFT_DBL')
            except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
                pass

    def wheelEvent(self, event):
        if self._scrub or not self._player:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        key = 'WHEEL_UP' if delta > 0 else 'WHEEL_DOWN'
        try:
            self._player.command('keypress', key)
        except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
            pass

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._scrub and self._player:
            dpr = self.devicePixelRatio()
            x = int(event.position().x() * dpr)
            y = int(event.position().y() * dpr)
            try:
                self._player.command('mouse', x, y)
            except Exception:  # why: mpv command() raises RuntimeError/OSError on terminated player
                pass

        if self._scrub:
            return

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

    # --------------------------------------------------------------- status bar

    @Slot()
    def _update_status(self):
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
        except Exception:  # why: mpv property reads raise on uninitialized or terminated player
            pass

    @staticmethod
    def _fmt(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # ---------------------------------------------------------------- lifecycle

    def _destroy_player(self):
        if not self._scrub and hasattr(self, '_status_timer'):
            self._status_timer.stop()
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="video_view",
                timestamp=time.time(),
                message="",
                section=StatusSection.PROCESS,
            ))
        self._has_first_frame = False
        if self._render_ctx:
            try:
                self.makeCurrent()
                self._render_ctx.free()
                self.doneCurrent()
            except Exception:  # why: GL context or render context may already be destroyed at shutdown
                pass
            self._render_ctx = None
        if self._player:
            try:
                self._player.terminate()
            except Exception:  # why: mpv handle may already be destroyed at shutdown
                pass
            self._player = None

    @property
    def video_widget(self):
        """Expose ourselves as the video widget for rendering"""
        return self

    def _apply_scaling(self):
        """Ensure video fits within thumbnail boundaries while maintaining aspect ratio."""
        w, h = self.width(), self.height()
        if w <= 1 or h <= 1:
            return

        # The player is already sized to match the thumbnail by the caller
        # Just ensure the video_widget (self) occupies the full space
        self.setFixedSize(w, h)

    def closeEvent(self, event):
        self._destroy_player()
        super().closeEvent(event)

    def play(self):
        """Unpause playback from the current position."""
        if self._player:
            try:
                self._player.pause = False
            except Exception:  # why: mpv property raises on terminated player
                pass

    def stop(self):
        """Stop video playback and clean up resources."""
        self._destroy_player()
