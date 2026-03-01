"""Non-modal ComfyUI generation dialog.

Triggered by the G hotkey.  Dynamically generates form controls from the
selected ComfyUI API workflow JSON — every editable parameter gets an
appropriate widget (spinbox, checkbox, text field, etc.).
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QComboBox,
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
import json
import os
import time

from .workflow_form import WorkflowFormWidget
from network.daemon_signals import DaemonSignals
from network.protocol import ComfyUICompleteData

_BUILTIN_LABEL = "(built-in: Flux Kontext)"

# Built-in Flux Kontext workflow exposed as a dict so the dialog can
# build a dynamic form without instantiating ComfyUIClient.
_BUILTIN_WORKFLOW: dict = {
    "1": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "flux1-dev-kontext_fp8_scaled.safetensors",
            "weight_dtype": "default",
        },
        "_meta": {"title": "Load Diffusion Model"},
    },
    "2": {
        "class_type": "DualCLIPLoader",
        "inputs": {
            "clip_name1": "clip_l.safetensors",
            "clip_name2": "t5xxl_fp8_e4m3fn_scaled.safetensors",
            "type": "flux",
            "device": "default",
        },
        "_meta": {"title": "DualCLIPLoader"},
    },
    "3": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "ae.safetensors"},
        "_meta": {"title": "Load VAE"},
    },
    "4": {
        "class_type": "LoadImage",
        "inputs": {"image": "placeholder.jpg"},
        "_meta": {"title": "Load Image"},
    },
    "5": {
        "class_type": "FluxKontextImageScale",
        "inputs": {"image": ["4", 0]},
        "_meta": {"title": "FluxKontextImageScale"},
    },
    "6": {
        "class_type": "VAEEncode",
        "inputs": {"pixels": ["5", 0], "vae": ["3", 0]},
        "_meta": {"title": "VAE Encode"},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["2", 0]},
        "_meta": {"title": "Prompt"},
    },
    "8": {
        "class_type": "ReferenceLatent",
        "inputs": {"conditioning": ["7", 0], "latent": ["6", 0]},
        "_meta": {"title": "ReferenceLatent"},
    },
    "9": {
        "class_type": "FluxGuidance",
        "inputs": {"guidance": 2.5, "conditioning": ["8", 0]},
        "_meta": {"title": "FluxGuidance"},
    },
    "10": {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["7", 0]},
        "_meta": {"title": "ConditioningZeroOut"},
    },
    "11": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 8,
            "cfg": 1.0,
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 0.30,
            "model": ["1", 0],
            "positive": ["9", 0],
            "negative": ["10", 0],
            "latent_image": ["6", 0],
        },
        "_meta": {"title": "KSampler"},
    },
    "12": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["11", 0], "vae": ["3", 0]},
        "_meta": {"title": "VAE Decode"},
    },
    "13": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "RabbitViewer", "images": ["12", 0]},
        "_meta": {"title": "Save Image"},
    },
}


class ComfyUIDialog(QDialog):

    generate_requested = Signal(str, str, float, str)  # (image_path, prompt, denoise, workflow_json)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ComfyUI Generate")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(440, 480)

        self._image_paths: list = []
        self._workflow_path = None  # None = built-in
        self._current_workflow: dict = {}  # raw workflow dict for patching
        self._daemon_signals: DaemonSignals | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Workflow:"))
        self._workflow_combo = QComboBox()
        self._workflow_combo.currentIndexChanged.connect(self._on_workflow_changed)
        layout.addWidget(self._workflow_combo)

        self._image_label = QLabel("Image: (none)")
        layout.addWidget(self._image_label)

        self._form = WorkflowFormWidget()
        layout.addWidget(self._form, stretch=1)

        self._generate_btn = QPushButton("Generate")
        self._generate_btn.clicked.connect(self._on_generate)
        layout.addWidget(self._generate_btn)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self.close)

    # ── Workflow selection ────────────────────────────────────────

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

        # Restore last-used workflow (keep signals blocked to avoid double load)
        last = self.parent().config_manager.get("comfyui.last_workflow", "")
        if last:
            idx = self._workflow_combo.findText(last)
            if idx >= 0:
                self._workflow_combo.setCurrentIndex(idx)
                self._workflow_path = self._workflow_combo.itemData(idx)
            else:
                self._workflow_path = None
        else:
            self._workflow_path = None

        self._workflow_combo.blockSignals(False)
        self._load_workflow()

    def _on_workflow_changed(self, index: int):
        if index <= 0:
            self._workflow_path = None
        else:
            self._workflow_path = self._workflow_combo.itemData(index)
        label = self._workflow_combo.itemText(index) if index > 0 else ""
        self.parent().config_manager.set("comfyui.last_workflow", label)
        self._load_workflow()

    def _load_workflow(self):
        """Load the selected workflow and rebuild the dynamic form."""
        if self._workflow_path:
            try:
                with open(self._workflow_path, "r") as f:
                    self._current_workflow = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self._status_label.setText(f"Error loading workflow: {e}")
                self._current_workflow = {}
                self._form.set_workflow({})
                return
        else:
            self._current_workflow = _BUILTIN_WORKFLOW

        self._form.set_workflow(self._current_workflow)

    # ── Actions ───────────────────────────────────────────────────

    def open_for_images(self, image_paths: list):
        self._image_paths = list(image_paths)
        n = len(self._image_paths)
        if n == 1:
            self._image_label.setText(f"Image: {os.path.basename(self._image_paths[0])}")
        else:
            self._image_label.setText(f"Images: {n} selected")
        self._status_label.setText("")
        self._generate_btn.setEnabled(True)
        self._populate_workflow_combo()
        self._reconnect()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_generate(self):
        if not self._image_paths or not self._current_workflow:
            return
        patched = self._form.patch_workflow(self._current_workflow)
        workflow_json = json.dumps(patched)
        for path in self._image_paths:
            self.generate_requested.emit(path, "", 0.0, workflow_json)
        self.close()

    # ── Notification handling ────────────────────────────────────

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._daemon_signals = daemon_signals
        daemon_signals.comfyui_complete.connect(self._on_comfyui_complete)

    def _reconnect(self) -> None:
        """Reconnect after a closeEvent disconnect, called from open_for_image."""
        if self._daemon_signals:
            try:
                self._daemon_signals.comfyui_complete.disconnect(self._on_comfyui_complete)
            except RuntimeError:
                pass
            self._daemon_signals.comfyui_complete.connect(self._on_comfyui_complete)

    @Slot(object)
    def _on_comfyui_complete(self, data: ComfyUICompleteData) -> None:
        if data.source_path not in self._image_paths:
            return
        if data.status != "success":
            from core.event_system import event_system, EventType, StatusMessageEventData
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="comfyui",
                timestamp=time.time(),
                message=f"ComfyUI error: {data.error or 'failed'} — {os.path.basename(data.source_path)}",
                timeout=8000,
            ))

    def closeEvent(self, event):
        if self._daemon_signals:
            try:
                self._daemon_signals.comfyui_complete.disconnect(self._on_comfyui_complete)
            except RuntimeError:
                pass
        super().closeEvent(event)
