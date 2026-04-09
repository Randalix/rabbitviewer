import logging
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QOpenGLContext

logger = logging.getLogger(__name__)

def _get_proc_address(_, name):
    ctx = QOpenGLContext.currentContext()
    if ctx is None:
        return 0
    return ctx.getProcAddress(name)

class VideoThumbnailPlayer(QOpenGLWidget):
    _mpv_frame_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(False)

        self._player = None
        self._render_ctx = None
        self._current_path: str | None = None
        self._duration: float = 0.0

        self._mpv_frame_ready.connect(self.update, Qt.QueuedConnection)

    def loadVideo(self, path: str) -> bool:
        if path == self._current_path and self._player:
            return True

        self.destroy_player()
        self._current_path = path
        self._duration = 0.0

        try:
            import mpv
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

            self.makeCurrent()
            from mpv import MpvRenderContext, MpvGlGetProcAddressFn
            proc_addr_fn = MpvGlGetProcAddressFn(_get_proc_address)
            self._proc_addr_fn = proc_addr_fn
            self._render_ctx = MpvRenderContext(
                self._player, 'opengl',
                opengl_init_params={'get_proc_address': proc_addr_fn},
            )
            self._render_ctx.update_cb = self._on_mpv_update
            self.doneCurrent()

            self._player.play(path)
            return True
        except Exception as e:
            logger.error(f"Failed to create mpv player for thumbnail: {e}", exc_info=True)
            self._current_path = None
            return False

    def seek_normalized(self, norm_x: float):
        if not self._player:
            return
        try:
            dur = self._player.duration
            if not dur or dur <= 0:
                self._player.observe_property('duration', self._on_duration_change)
                return
            self._duration = dur
            target = max(0.0, min(norm_x * dur, dur))
            self._player.seek(target, reference="absolute", precision="exact")
        except Exception:
            pass

    def _on_duration_change(self, name, value):
        if value and value > 0:
            self._duration = value
            self._player.unobserve_property('duration', self._on_duration_change)

    def _on_mpv_update(self):
        self._mpv_frame_ready.emit()

    def paintGL(self):
        if self._render_ctx:
            fbo = self.defaultFramebufferObject()
            w = int(self.width() * self.devicePixelRatio())
            h = int(self.height() * self.devicePixelRatio())
            self._render_ctx.render(
                opengl_fbo={'w': w, 'h': h, 'fbo': fbo},
                flip_y=True,
            )
            self._render_ctx.report_swap()

    def destroy_player(self):
        if self._render_ctx:
            try:
                self.makeCurrent()
                self._render_ctx.free()
                self.doneCurrent()
            except Exception:
                pass
            self._render_ctx = None
        if self._player:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        self._current_path = None

    def closeEvent(self, event):
        self.destroy_player()
        super().closeEvent(event)
