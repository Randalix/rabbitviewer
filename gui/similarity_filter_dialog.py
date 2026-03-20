import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QSlider, QVBoxLayout,
)

_MODES = ["CLIP", "pHash"]

# Per-mode slider config: (min, max, default, label, scale)
# Both modes emit a 0.0–1.0 similarity value: 0 = open filter, 1 = close match.
# scale: multiplier from raw slider int to emitted float value.
_MODE_CONFIG = {
    "CLIP":  dict(min=0, max=100, default=50, label="Similarity:", scale=0.01),
    "pHash": dict(min=0, max=100, default=84, label="Similarity:", scale=0.01),
}


class SimilarityFilterDialog(QDialog):
    """Both modes emit a 0.0–1.0 similarity value: 0 = open filter, 1 = close match.
    Emits params_changed(mode, value) on every slider/mode change.
    Emits filter_cleared on close.
    """

    params_changed = Signal(str, float)  # (mode, value)
    filter_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Similarity Filter")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)

        self._source_label = QLabel("No image selected")
        self._source_label.setWordWrap(True)
        layout.addWidget(self._source_label)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(_MODES)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        slider_row = QHBoxLayout()
        self._slider_label = QLabel(_MODE_CONFIG["CLIP"]["label"])
        self._slider_label.setFixedWidth(90)
        slider_row.addWidget(self._slider_label)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setTickPosition(QSlider.TicksBelow)
        self._slider.setTickInterval(10)
        slider_row.addWidget(self._slider)
        self._value_label = QLabel()
        self._value_label.setFixedWidth(40)
        slider_row.addWidget(self._value_label)
        layout.addLayout(slider_row)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        self._apply_mode_config("CLIP", emit=False)

        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self._slider.valueChanged.connect(self._on_slider_changed)
        self.adjustSize()

        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

    # -- Public API ----------------------------------------------------------

    def set_source(self, path: str):
        self._source_label.setText(f"Similar to: {os.path.basename(path)}")

    def set_result_count(self, count: int):
        self._count_label.setText(f"{count} matching images")

    def set_error(self, message: str):
        self._count_label.setText(message)

    @property
    def current_params(self) -> tuple:
        mode = self._mode_combo.currentText()
        return mode, self._slider.value() * _MODE_CONFIG[mode]["scale"]

    # -- Internal ------------------------------------------------------------

    def _apply_mode_config(self, mode: str, emit: bool = True):
        cfg = _MODE_CONFIG[mode]
        self._slider.blockSignals(True)
        self._slider.setMinimum(cfg["min"])
        self._slider.setMaximum(cfg["max"])
        self._slider.setTickInterval(max(1, (cfg["max"] - cfg["min"]) // 8))
        self._slider.setValue(cfg["default"])
        self._slider.blockSignals(False)
        self._slider_label.setText(cfg["label"])
        self._update_value_label(mode, cfg["default"])
        if emit:
            self.params_changed.emit(mode, cfg["default"] * cfg["scale"])

    def _update_value_label(self, mode: str, raw: int):
        value = raw * _MODE_CONFIG[mode]["scale"]
        self._value_label.setText(f"{value:.2f}")

    def _on_mode_changed(self, mode: str):
        self._count_label.setText("")
        self._apply_mode_config(mode, emit=True)

    def _on_slider_changed(self, raw: int):
        mode = self._mode_combo.currentText()
        self._update_value_label(mode, raw)
        self.params_changed.emit(mode, raw * _MODE_CONFIG[mode]["scale"])

