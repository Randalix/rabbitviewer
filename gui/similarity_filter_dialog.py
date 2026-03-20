import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QSlider, QVBoxLayout,
)


class SimilarityFilterDialog(QDialog):
    """Filter images by visual similarity to a source image.

    Emits threshold_changed whenever the slider moves; caller is responsible
    for running the background search and calling set_result_count().
    Emits filter_cleared on close so the caller can tear down the filter.
    """

    threshold_changed = Signal(float)  # 0.0–1.0
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

        row = QHBoxLayout()
        row.addWidget(QLabel("Threshold:"))
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(100)
        self._slider.setValue(50)
        self._slider.setTickPosition(QSlider.TicksBelow)
        self._slider.setTickInterval(10)
        row.addWidget(self._slider)
        self._threshold_label = QLabel("0.50")
        self._threshold_label.setFixedWidth(40)
        row.addWidget(self._threshold_label)
        layout.addLayout(row)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        self._slider.valueChanged.connect(self._on_slider_changed)
        self.adjustSize()

        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

    def set_source(self, path: str):
        self._source_label.setText(f"Similar to: {os.path.basename(path)}")

    def set_result_count(self, count: int):
        self._count_label.setText(f"{count} matching images")

    def set_error(self, message: str):
        self._count_label.setText(message)

    @property
    def threshold(self) -> float:
        return self._slider.value() / 100.0

    def _on_slider_changed(self, value: int):
        threshold = value / 100.0
        self._threshold_label.setText(f"{threshold:.2f}")
        self.threshold_changed.emit(threshold)

    def closeEvent(self, event):
        self.filter_cleared.emit()
        super().closeEvent(event)
