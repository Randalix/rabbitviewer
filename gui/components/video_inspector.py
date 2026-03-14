import logging

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt

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

    @property
    def current_path(self) -> str | None:
        return self._current_path

    def load_video(self, path: str) -> bool:
        self._current_path = path
        return self._video_view.loadVideo(path)

    def seek_normalized(self, norm_x: float):
        self._video_view.seek_normalized(norm_x)

    def destroy_player(self):
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
        if self._is_panning:
            self._scrub_at(event)
        super().mouseMoveEvent(event)

    def _scrub_at(self, event):
        w = self.width()
        if w <= 0 or not self._current_path:
            return
        norm_x = max(0.0, min(1.0, event.position().x() / w))
        self._video_view.seek_normalized(norm_x)
