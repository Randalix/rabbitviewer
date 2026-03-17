# gui/inspector_view.py

import logging
import os

from PySide6.QtWidgets import QWidget, QStackedLayout
from PySide6.QtCore import QPointF, QSettings, Signal, Slot
from core.event_system import event_system, EventType, InspectorEventData
from network.daemon_signals import DaemonSignals
from core.notifications import PreviewsReadyData
from plugins.video_plugin import VIDEO_EXTENSIONS
from .components.image_inspector import ImageInspector, ViewMode
from .components.video_inspector import VideoInspector

logger = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = frozenset(VIDEO_EXTENSIONS)


def _is_video(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lower() in _VIDEO_EXTENSIONS


class InspectorView(QWidget):

    closed = Signal()

    def __init__(self, config_manager=None, inspector_index: int = 0, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._inspector_index = inspector_index
        self._cleaned_up = False

        self._current_image_path: str | None = None
        self._desired_image_path: str | None = None
        self._desired_norm_pos: QPointF = QPointF(0.5, 0.5)
        self._is_video_mode: bool = False
        self._pinned: bool = False

        # --- child views ---
        self._image_inspector = ImageInspector(parent=self)
        self._video_inspector = VideoInspector(parent=self)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._image_inspector)
        self._stack.addWidget(self._video_inspector)
        self._stack.setCurrentWidget(self._image_inspector)

        self._image_inspector.view_mode_changed.connect(self._update_window_title)

        # --- widget setup ---
        self.setMinimumSize(200, 200)

        # why: zoom_factor is a global preference (shared across inspector windows);
        # per-index QSettings keys (zoom_factor_0, zoom_factor_1, ...) are now orphaned.
        if self.config_manager:
            zoom = float(self.config_manager.get("inspector.zoom_factor", 3.0))
        else:
            zoom = 3.0
        self._image_inspector.set_zoom_factor(zoom)

        settings = QSettings("RabbitViewer", "Inspector")
        try:
            view_mode_str = settings.value(f"view_mode_{self._inspector_index}", ViewMode.TRACKING.value)
            self._image_inspector.view_mode = ViewMode(view_mode_str)
        except ValueError:
            self._image_inspector.view_mode = ViewMode.TRACKING

        event_system.subscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)

        self.service = None
        self._daemon_signals: DaemonSignals | None = None

    # ------------------------------------------------------------------ public API

    def set_service(self, service):
        self.service = service
        self._image_inspector.service = service

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._daemon_signals = daemon_signals
        daemon_signals.previews_ready.connect(self._on_previews_ready)

    def toggle_pin(self):
        self._pinned = not self._pinned
        if not self._pinned and not self._is_video_mode:
            # Unpin: catch up to the current hover target if it differs.
            if (self._desired_image_path
                    and self._desired_image_path != self._current_image_path
                    and self.service):
                self._image_inspector.handle_update(
                    self._desired_image_path, self._desired_norm_pos)
        self._update_window_title()

    def zoom_by_factor(self, factor: float):
        if not self._is_video_mode:
            self._image_inspector.zoom_by_factor(factor)

    def prime(self, event_data: InspectorEventData):
        self._handle_inspector_update(event_data)

    # ------------------------------------------------------------------ event dispatch

    def _handle_inspector_update(self, event_data: InspectorEventData):
        if not self.isVisible():
            return

        image_path = event_data.image_path
        norm_pos = event_data.normalized_position

        # ------ PINNED: ignore image changes, still track position ------
        if self._pinned and image_path != self._current_image_path:
            self._desired_norm_pos = norm_pos
            if not self._is_video_mode and self._image_inspector.view_mode == ViewMode.TRACKING:
                self._image_inspector.set_center(norm_pos)
            return

        # ------ VIDEO PATH ------
        if _is_video(image_path):
            if not self._is_video_mode:
                self._is_video_mode = True
                self._stack.setCurrentWidget(self._video_inspector)
            self._desired_image_path = image_path
            self._desired_norm_pos = norm_pos
            self._current_image_path = image_path

            self._video_inspector.load_video(image_path)
            self._video_inspector.seek_normalized(norm_pos.x())
            self._update_window_title()
            return

        # ------ IMAGE PATH ------
        if self._is_video_mode:
            self._is_video_mode = False
            self._stack.setCurrentWidget(self._image_inspector)
            self._update_window_title()

        self._desired_image_path = image_path
        self._desired_norm_pos = norm_pos
        self._current_image_path = image_path
        cache_only = event_data.cache_only
        self._image_inspector.handle_update(image_path, norm_pos, cache_only=cache_only)

    @Slot(object)
    def _on_previews_ready(self, data: PreviewsReadyData) -> None:
        if self._is_video_mode:
            return
        target = data.image_entry.path
        norm_pos = self._desired_norm_pos if self._image_inspector.view_mode == ViewMode.TRACKING else QPointF(0.5, 0.5)
        self._image_inspector.handle_previews_ready(
            target, data.view_image_path, data.view_image_source or "", norm_pos)

    # ------------------------------------------------------------------ title (unused when tiled, kept for tooltip/debug)

    def _update_window_title(self):
        pass

    # ------------------------------------------------------------------ lifecycle

    def showEvent(self, event):
        super().showEvent(event)
        self._image_inspector.reset_fetches()

    def cleanup(self):
        """Tear down resources. Called by MainWindow when removing from the panel."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._image_inspector.cancel_fetches()
        self._video_inspector.destroy_player()
        settings = QSettings("RabbitViewer", "Inspector")
        settings.setValue(f"view_mode_{self._inspector_index}", self._image_inspector.view_mode.value)
        settings.sync()
        if self.config_manager:
            self.config_manager.set("inspector.zoom_factor", self._image_inspector.zoom_factor)
        self._pinned = False
        event_system.unsubscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_update)
        if self._daemon_signals:
            self._daemon_signals.previews_ready.disconnect(self._on_previews_ready)
        self.closed.emit()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
