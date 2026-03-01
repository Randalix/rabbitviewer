"""Dynamic form widget that generates Qt controls from a ComfyUI API workflow.

Parses node inputs and creates appropriate widgets: checkboxes for bools,
spinboxes for numbers, text fields for strings.  Node connections (array
inputs) and infrastructure nodes (loaders, save/preview) are filtered out.
"""

import copy
from typing import Any, Dict, Optional, Tuple

from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLineEdit, QScrollArea, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

# Nodes skipped entirely — not user-editable creative parameters.
_SKIP_CLASS_TYPES = frozenset({
    "LoadImage",
    "SaveImage",
    "PreviewImage",
    # Model / checkpoint loaders
    "UNETLoader",
    "CheckpointLoaderSimple",
    "VAELoader",
    "DualCLIPLoader",
    "UpscaleModelLoader",
    "DownloadAndLoadDepthAnythingV2Model",
})


def _is_link(value: Any) -> bool:
    """True if *value* is a ComfyUI node connection ``[node_id, output_idx]``."""
    return isinstance(value, list) and len(value) == 2


def _node_title(node: dict) -> str:
    meta = node.get("_meta", {})
    return meta.get("title") or node.get("class_type", "Node")


def _make_widget(
    name: str, value: Any, class_type: str
) -> Optional[Tuple[QWidget, str]]:
    """Return ``(widget, display_label)`` for an editable input, or *None*.

    *class_type* is the owning node's class so we can special-case prompts.
    """
    if _is_link(value):
        return None

    label = name.replace("_", " ")

    if isinstance(value, bool):
        cb = QCheckBox()
        cb.setChecked(value)
        return cb, label

    if isinstance(value, int):
        if name == "seed":
            return _make_seed_widget(value), label
        sb = QSpinBox()
        lo = min(value, 0)
        hi = max(value * 10, 10_000)
        # Clamp to int32 range for QSpinBox
        lo = max(lo, -(2**31))
        hi = min(hi, 2**31 - 1)
        sb.setRange(lo, hi)
        sb.setValue(value)
        return sb, label

    if isinstance(value, float):
        dsb = QDoubleSpinBox()
        if 0.0 <= value <= 1.0:
            dsb.setRange(0.0, 1.0)
            dsb.setSingleStep(0.01)
            dsb.setDecimals(2)
        else:
            dsb.setRange(0.0, max(value * 10, 100.0))
            dsb.setSingleStep(0.1)
            dsb.setDecimals(2)
        dsb.setValue(value)
        return dsb, label

    if isinstance(value, str):
        if class_type == "CLIPTextEncode" and name == "text":
            te = QTextEdit()
            te.setPlainText(value)
            te.setMaximumHeight(80)
            return te, label
        le = QLineEdit(value)
        return le, label

    return None


def _make_seed_widget(value: int) -> QWidget:
    """SpinBox + 'Randomize' checkbox (checked by default)."""
    container = QWidget()
    lay = QHBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)

    sb = QSpinBox()
    sb.setRange(0, 2**31 - 1)
    # Clamp to int32 — original seed is just a starting point and
    # "Random" is on by default so the exact value rarely matters.
    sb.setValue(min(value, 2**31 - 1))
    sb.setEnabled(False)  # disabled when randomize is on

    cb = QCheckBox("Random")
    cb.setChecked(True)
    cb.toggled.connect(lambda on: sb.setEnabled(not on))

    lay.addWidget(sb)
    lay.addWidget(cb)

    # Stash references for value retrieval.
    container._spinbox = sb
    container._random_cb = cb
    return container


def _read_widget_value(widget: QWidget) -> Any:
    """Extract the current value from a dynamically-created widget."""
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QDoubleSpinBox):
        return widget.value()
    if isinstance(widget, QTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QLineEdit):
        return widget.text()
    # Seed container
    if hasattr(widget, "_random_cb"):
        if widget._random_cb.isChecked():
            return None  # signals "randomize at runtime"
        return widget._spinbox.value()
    return None


class WorkflowFormWidget(QScrollArea):
    """Scroll area that dynamically generates controls for a ComfyUI workflow."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._widgets: Dict[str, Dict[str, QWidget]] = {}  # node_id → {input_name → widget}
        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setAlignment(Qt.AlignTop)
        self.setWidget(self._inner)

    def set_workflow(self, workflow: dict) -> None:
        """Parse *workflow* and rebuild all form controls."""
        self._clear()
        self._widgets.clear()

        for node_id, node in workflow.items():
            if not isinstance(node, dict) or "class_type" not in node:
                continue
            class_type = node["class_type"]
            if class_type in _SKIP_CLASS_TYPES:
                continue

            inputs = node.get("inputs", {})
            node_widgets: Dict[str, QWidget] = {}

            for inp_name, inp_val in inputs.items():
                result = _make_widget(inp_name, inp_val, class_type)
                if result is not None:
                    node_widgets[inp_name] = result  # (widget, label)

            if not node_widgets:
                continue

            title = _node_title(node)
            group = QGroupBox(title)
            form = QFormLayout(group)
            for inp_name, (widget, label) in node_widgets.items():
                form.addRow(f"{label}:", widget)

            self._layout.addWidget(group)
            self._widgets[node_id] = {k: v[0] for k, v in node_widgets.items()}

    def get_values(self) -> Dict[str, Dict[str, Any]]:
        """Return ``{node_id: {input_name: value}}`` for all editable inputs.

        Seed inputs return *None* when "Randomize" is checked.
        """
        result: Dict[str, Dict[str, Any]] = {}
        for node_id, inputs in self._widgets.items():
            vals: Dict[str, Any] = {}
            for inp_name, widget in inputs.items():
                vals[inp_name] = _read_widget_value(widget)
            result[node_id] = vals
        return result

    def patch_workflow(self, workflow: dict) -> dict:
        """Return a deep copy of *workflow* with user-edited values applied."""
        patched = copy.deepcopy(workflow)
        for node_id, vals in self.get_values().items():
            node = patched.get(node_id)
            if node is None:
                continue
            inputs = node.get("inputs", {})
            for inp_name, val in vals.items():
                if val is None:
                    continue  # seed=None means "randomize later"
                inputs[inp_name] = val
        return patched

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
