"""Standalone tag filter dialog.

Opened via the Tags menu (T → F).  Provides a comma-separated tag input
with two-tier autocomplete.  Emits `tags_changed` when the user commits
a filter, and clears the filter when toggled off.
"""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from gui.components.tag_input import TagInput


class TagFilterDialog(QDialog):
    """Non-modal dialog for filtering images by tags."""

    tags_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tag Filter")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(400, 100)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Filter by tags:"))

        self.tag_input = TagInput()
        self.tag_input.tags_changed.connect(self._on_tags_changed)
        self.tag_input.confirmed.connect(self._on_confirmed)
        layout.addWidget(self.tag_input)

        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self.close)

    def set_available_tags(self, directory_tags: list, global_tags: list):
        self.tag_input.set_available_tags(directory_tags, global_tags)

    def _on_tags_changed(self, tags: list):
        self.tags_changed.emit(tags)

    def _on_confirmed(self):
        self.tags_changed.emit(self.tag_input.get_tags())
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self.tag_input.setFocus()
        self.tag_input.selectAll()

    def clear_filter(self):
        self.tag_input.clear()
        self.tags_changed.emit([])

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Tab:
            focused = self.focusWidget()
            if isinstance(focused, TagInput):
                focused.keyPressEvent(event)
            return
        super().keyPressEvent(event)
