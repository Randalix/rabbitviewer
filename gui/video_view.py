import logging
import time

from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent, QOpenGLContext

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

        self._mpv_frame_ready.connect(self.update, Qt.QueuedConnection)

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
        pass  # Deferred to loadVideo — context created on first play.

    def loadVideo(self, path: str) -> bool:
        if path == self._current_path and self._player:
            return True

        self._destroy_player()
        self._current_path = path
        self._duration = 0.0

        try:
            import mpv

            if self._scrub:
                self._player = mpv.MPV(
                    vo="libmpv",
                    input_default_bindings=False,
                    input_vo_keyboard=False,
                    keep_open="yes",
                    hwdec="auto-safe",
                    hr_seek="yes",
                    pause=True,
                    aid="no",
                )
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

    def seek_normalized(self, norm_x: float):
        if not self._player:
            return
        try:
            dur = self._player.duration
            if not dur or dur <= 0:
                return
            self._duration = dur
            target = max(0.0, min(norm_x * dur, dur))
            self._player.seek(target, reference="absolute", precision="exact")
        except Exception:  # why: mpv property/command raises on terminated player
            pass

    # --------------------------------------------------------- mpv render callback

    def _on_mpv_update(self):
        self._mpv_frame_ready.emit()

    def paintGL(self):
        if not self._render_ctx:
            return
        fbo = self.defaultFramebufferObject()
        w = int(self.width() * self.devicePixelRatio())
        h = int(self.height() * self.devicePixelRatio())
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

    def closeEvent(self, event):
        self._destroy_player()
        super().closeEvent(event)
