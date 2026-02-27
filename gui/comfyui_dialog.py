"""Non-modal ComfyUI generation dialog.

Triggered by the G hotkey.  Exposes prompt and denoise controls,
sends a generate request to the daemon, and displays status updates.

Subscribes to DAEMON_NOTIFICATION directly and handles comfyui_complete
notifications internally (same pattern as InspectorView/PictureView).
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QSlider, QPushButton, QComboBox,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
import json
import logging
import os

from core.event_system import event_system, EventType

_BUILTIN_LABEL = "(built-in: Flux Kontext)"


class ComfyUIDialog(QDialog):

    generate_requested = Signal(str, str, float, str)  # (image_path, prompt, denoise, workflow_json)
    _daemon_notification = Signal(object)  # thread→GUI bridge

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ComfyUI Generate")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(420, 290)

        self._image_path = ""
        self._workflow_path = None  # None = built-in
        self._subscribed = True

        self._daemon_notification.connect(self._process_notification)
        event_system.subscribe(EventType.DAEMON_NOTIFICATION,
                               self._on_daemon_notification_from_thread)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Workflow:"))
        self._workflow_combo = QComboBox()
        self._workflow_combo.currentIndexChanged.connect(self._on_workflow_changed)
        layout.addWidget(self._workflow_combo)

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

    def _resolve_workflows_dir(self) -> str:
        """Return the workflows directory, creating it if needed."""
        cm = self.parent().config_manager
        custom = cm.get("comfyui.workflows_dir", "")
        if custom:
            d = os.path.expanduser(custom)
        else:
            config_dir = os.path.dirname(cm.config_path)
            d = os.path.join(config_dir, "workflows")
        os.makedirs(d, exist_ok=True)
        return d

    def _populate_workflow_combo(self):
        """Scan the workflows dir and repopulate the combo box."""
        self._workflow_combo.blockSignals(True)
        self._workflow_combo.clear()
        self._workflow_combo.addItem(_BUILTIN_LABEL)

        workflows_dir = self._resolve_workflows_dir()
        try:
            files = sorted(
                f for f in os.listdir(workflows_dir)
                if f.lower().endswith(".json")
            )
        except OSError:
            files = []
        for f in files:
            name = os.path.splitext(f)[0]
            full_path = os.path.join(workflows_dir, f)
            self._workflow_combo.addItem(name, userData=full_path)

        self._workflow_combo.blockSignals(False)

        # Restore last-used workflow
        last = self.parent().config_manager.get("comfyui.last_workflow", "")
        if last:
            idx = self._workflow_combo.findText(last)
            if idx >= 0:
                self._workflow_combo.setCurrentIndex(idx)
                self._workflow_path = self._workflow_combo.itemData(idx)
                return
        self._workflow_path = None

    def _on_workflow_changed(self, index: int):
        if index <= 0:
            self._workflow_path = None
        else:
            self._workflow_path = self._workflow_combo.itemData(index)
        label = self._workflow_combo.itemText(index) if index > 0 else ""
        self.parent().config_manager.set("comfyui.last_workflow", label)

    def open_for_image(self, image_path: str):
        self._image_path = image_path
        self._image_label.setText(f"Image: {os.path.basename(image_path)}")
        self._status_label.setText("")
        self._generate_btn.setEnabled(True)
        self._populate_workflow_combo()
        self._resubscribe()
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
        workflow_json = ""
        if self._workflow_path:
            try:
                with open(self._workflow_path, "r") as f:
                    workflow_json = f.read()
                # Validate JSON before sending
                json.loads(workflow_json)
            except (OSError, json.JSONDecodeError) as e:
                self._status_label.setText(f"Error loading workflow: {e}")
                return
        self._generate_btn.setEnabled(False)
        self._status_label.setText("Status: Generating...")
        self.generate_requested.emit(self._image_path, prompt, denoise, workflow_json)

    # ── Notification handling ────────────────────────────────────

    def _resubscribe(self):
        """Re-subscribe to daemon notifications if previously unsubscribed (e.g. after close)."""
        if not self._subscribed:
            event_system.subscribe(EventType.DAEMON_NOTIFICATION,
                                   self._on_daemon_notification_from_thread)
            self._subscribed = True

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
