"""Non-modal ComfyUI generation dialog.

Triggered by the G hotkey.  Exposes prompt and denoise controls,
sends a generate request to the daemon, and displays status updates.

Subscribes to DAEMON_NOTIFICATION directly and handles comfyui_complete
notifications internally (same pattern as InspectorView/PictureView).
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QSlider, QPushButton,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
import logging
import os

from core.event_system import event_system, EventType


class ComfyUIDialog(QDialog):

    generate_requested = Signal(str, str, float)  # (image_path, prompt, denoise)
    _daemon_notification = Signal(object)  # thread→GUI bridge

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ComfyUI Generate")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(420, 260)

        self._image_path = ""
        self._subscribed = True

        self._daemon_notification.connect(self._process_notification)
        event_system.subscribe(EventType.DAEMON_NOTIFICATION,
                               self._on_daemon_notification_from_thread)

        layout = QVBoxLayout(self)

        self._image_label = QLabel("Image: (none)")
        layout.addWidget(self._image_label)

        layout.addWidget(QLabel("Prompt:"))
        self._prompt_input = QTextEdit()
        self._prompt_input.setPlaceholderText("Describe the edit to apply...")
        self._prompt_input.setMaximumHeight(80)
        layout.addWidget(self._prompt_input)

        denoise_row = QHBoxLayout()
        denoise_row.addWidget(QLabel("Denoise:"))
        self._denoise_slider = QSlider(Qt.Horizontal)
        self._denoise_slider.setRange(0, 100)
        self._denoise_slider.setValue(30)
        self._denoise_slider.valueChanged.connect(self._update_denoise_label)
        denoise_row.addWidget(self._denoise_slider)
        self._denoise_label = QLabel("0.30")
        self._denoise_label.setFixedWidth(36)
        denoise_row.addWidget(self._denoise_label)
        layout.addLayout(denoise_row)

        self._generate_btn = QPushButton("Generate")
        self._generate_btn.clicked.connect(self._on_generate)
        layout.addWidget(self._generate_btn)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self.close)

    def open_for_image(self, image_path: str):
        self._image_path = image_path
        self._image_label.setText(f"Image: {os.path.basename(image_path)}")
        self._status_label.setText("")
        self._generate_btn.setEnabled(True)
        self.show()
        self.raise_()
        self.activateWindow()
        self._prompt_input.setFocus()

    def set_status(self, text: str):
        self._status_label.setText(text)
        self._generate_btn.setEnabled(True)

    def _update_denoise_label(self, value: int):
        self._denoise_label.setText(f"{value / 100:.2f}")

    def _on_generate(self):
        prompt = self._prompt_input.toPlainText().strip()
        if not prompt or not self._image_path:
            return
        denoise = self._denoise_slider.value() / 100.0
        self._generate_btn.setEnabled(False)
        self._status_label.setText("Status: Generating...")
        self.generate_requested.emit(self._image_path, prompt, denoise)

    # ── Notification handling ────────────────────────────────────

    def _on_daemon_notification_from_thread(self, event_data):
        # why: notification arrives on NotificationClient thread; signal bridges to GUI thread
        if event_data.notification_type == "comfyui_complete":
            self._daemon_notification.emit(event_data)

    def _process_notification(self, event_data):
        data = event_data.data
        from network.protocol import ComfyUICompleteData
        try:
            complete = ComfyUICompleteData.model_validate(data)
        except (KeyError, TypeError, ValueError):  # why: daemon payload may be malformed
            self.set_status("Error: malformed response")
            return
        if complete.source_path != self._image_path:
            return
        if complete.status == "success" and complete.result_path:
            basename = os.path.basename(complete.result_path)
            self.set_status(f"Complete: {basename}")
        else:
            error_msg = complete.error or "Generation failed"
            self.set_status(f"Error: {error_msg}")

    def _unsubscribe(self):
        if self._subscribed:
            event_system.unsubscribe(EventType.DAEMON_NOTIFICATION,
                                     self._on_daemon_notification_from_thread)
            self._subscribed = False

    def closeEvent(self, event):
        self._unsubscribe()
        super().closeEvent(event)

    def __del__(self):
        # why: guard against Qt parent destruction skipping closeEvent
        self._unsubscribe()
