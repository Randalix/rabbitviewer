"""CLIP semantic search dialog.

Provides a text input and similarity threshold slider for natural language image search
using CLIP embeddings. Behaves as a pure filter (no reordering), consistent with
SimilarityFilterDialog.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, QSlider,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

_DEFAULT_THRESHOLD_INT = 23   # slider integer → 0.23 cosine similarity
_SCALE = 0.01


class ClipSearchDialog(QDialog):
    """Non-modal dialog for CLIP semantic image search.

    Emits search_requested(query) when the user submits a query.
    Emits threshold_changed(float) when the slider moves while a query is active.
    Emits filter_cleared() on close.
    """

    search_requested = Signal(str)
    threshold_changed = Signal(float)
    filter_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CLIP Search")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self._query_active = False
        self._setup_ui()
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Search images by description:"))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("e.g. sunset over ocean, red car, portrait...")
        self.search_input.returnPressed.connect(self._on_search)
        layout.addWidget(self.search_input)

        slider_row = QHBoxLayout()
        self._slider_label = QLabel("Min similarity:")
        self._slider_label.setFixedWidth(90)
        slider_row.addWidget(self._slider_label)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(100)
        self._slider.setValue(_DEFAULT_THRESHOLD_INT)
        self._slider.setTickPosition(QSlider.TicksBelow)
        self._slider.setTickInterval(10)
        slider_row.addWidget(self._slider)
        self._value_label = QLabel(f"{_DEFAULT_THRESHOLD_INT * _SCALE:.2f}")
        self._value_label.setFixedWidth(40)
        slider_row.addWidget(self._value_label)
        layout.addLayout(slider_row)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        self._slider.valueChanged.connect(self._on_slider_changed)
        self.search_input.setFocus()
        self.adjustSize()

    @property
    def threshold(self) -> float:
        return self._slider.value() * _SCALE

    def set_result_count(self, count: int):
        self._count_label.setText(f"{count} matching images")

    def set_error(self, message: str):
        self._count_label.setText(message)

    def clear_search(self):
        self.search_input.clear()
        self._count_label.setText("")
        self._query_active = False

    def showEvent(self, event):
        super().showEvent(event)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def closeEvent(self, event):
        self._query_active = False
        self._count_label.setText("")
        self.filter_cleared.emit()
        super().closeEvent(event)

    def _on_search(self):
        query = self.search_input.text().strip()
        if query:
            self._query_active = True
            self._count_label.setText("Searching...")
            self.search_requested.emit(query)
        else:
            self._query_active = False
            self._count_label.setText("")
            self.filter_cleared.emit()

    def _on_slider_changed(self, raw: int):
        self._value_label.setText(f"{raw * _SCALE:.2f}")
        if self._query_active:
            self.threshold_changed.emit(raw * _SCALE)
