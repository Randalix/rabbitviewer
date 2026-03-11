"""CLIP semantic search dialog.

Provides a text input for natural language image search using CLIP embeddings.
"""
import os

from PySide6.QtWidgets import QDialog, QVBoxLayout, QLineEdit, QLabel, QListWidget, QListWidgetItem
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
import logging

logger = logging.getLogger(__name__)


class ClipSearchDialog(QDialog):
    """Non-modal dialog for CLIP semantic image search."""

    search_requested = Signal(str)
    result_selected = Signal(str)  # file_path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CLIP Search")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(500, 400)
        self._results = []
        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        label = QLabel("Search images by description:")
        layout.addWidget(label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("e.g. sunset over ocean, red car, portrait...")
        self.search_input.returnPressed.connect(self._on_search)
        layout.addWidget(self.search_input)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.results_list)

        self.search_input.setFocus()

    def _setup_shortcuts(self):
        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self.close)

    def _on_search(self):
        query = self.search_input.text().strip()
        if query:
            self.status_label.setText("Searching...")
            self.search_requested.emit(query)

    def _on_item_double_clicked(self, item: QListWidgetItem):
        idx = self.results_list.row(item)
        if 0 <= idx < len(self._results):
            file_path = self._results[idx][0]
            self.result_selected.emit(file_path)

    def set_results(self, results):
        """Update the results list. results: [(file_path, score), ...]"""
        self._results = results
        self.results_list.clear()
        for file_path, score in results:
            name = os.path.basename(file_path)
            self.results_list.addItem(f"{score:.3f}  {name}")
        self.status_label.setText(f"{len(results)} results")

    def set_error(self, message: str):
        self.status_label.setText(message)
        self.results_list.clear()
        self._results = []

    def showEvent(self, event):
        super().showEvent(event)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def clear_search(self):
        self.search_input.clear()
        self.results_list.clear()
        self._results = []
        self.status_label.setText("")
