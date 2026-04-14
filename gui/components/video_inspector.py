import logging

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QTimer

from gui.video_view import VideoView

logger = logging.getLogger(__name__)


class VideoInspector(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._video_view = VideoView(scrub=True, parent=self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._video_view)

        self._current_path: str | None = None
        self._is_panning = False

        self.setMouseTracking(True)
        self._video_view.setMouseTracking(True)

        self._play_timer = QTimer(self)
        self._play_timer.setSingleShot(True)
        self._play_timer.setInterval(500)
        self._play_timer.timeout.connect(self._video_view.play)

    @property
    def current_path(self) -> str | None:
        return self._current_path

    def load_video(self, path: str) -> bool:
        self._current_path = path
        result = self._video_view.loadVideo(path)
        self._play_timer.start()
        return result

    def seek_normalized(self, norm_x: float):
        self._video_view.seek_normalized(norm_x)

    def destroy_player(self):
        self._play_timer.stop()
        self._video_view.close()

    # ------------------------------------------------------------------ input
    # Manual scrub: left-drag maps mouse X to timeline position.

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._scrub_at(event)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_panning = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        self._play_timer.start()  # reset 500ms countdown on any movement
        if self._is_panning:
            self._scrub_at(event)
        super().mouseMoveEvent(event)

    def _scrub_at(self, event):
        w = self.width()
        if w <= 0 or not self._current_path:
            return
        norm_x = max(0.0, min(1.0, event.position().x() / w))
        self._video_view.seek_normalized(norm_x)
