"""Tag assignment popup for selected images.

Opened via the T hotkey.  Pre-populates with existing tags for the
selection, lets the user add/remove via a comma-separated input with
autocomplete, then diffs against the original set on confirm.
"""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
import logging

from gui.components.tag_input import TagInput


class TagEditorDialog(QDialog):
    """Non-modal dialog for assigning tags to selected images."""

    tags_confirmed = Signal(list, list)  # (tags_to_add, tags_to_remove)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Tags")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(400, 120)

        self._original_tags: set[str] = set()

        layout = QVBoxLayout(self)
        self._label = QLabel("Tags for selection:")
        layout.addWidget(self._label)

        self.tag_input = TagInput()
        self.tag_input.tags_changed.connect(self._on_confirm)
        self.tag_input.confirmed.connect(self._on_enter_confirmed)
        layout.addWidget(self.tag_input)

        escape = QShortcut(QKeySequence("Esc"), self)
        escape.activated.connect(self.close)

    def open_for_images(self, image_count: int, existing_tags: list[str],
                        directory_tags: list[str], global_tags: list[str]) -> None:
        """Populate and show the dialog."""
        self._original_tags = set(existing_tags)
        self._label.setText(f"Tags for {image_count} image(s):")
        self.tag_input.set_available_tags(directory_tags, global_tags)
        self.tag_input.set_tags(existing_tags)
        self.show()
        self.raise_()
        self.activateWindow()
        self.tag_input.setFocus()

    def _on_confirm(self, current_tags: list):
        """Compute diff and emit."""
        current_set = set(current_tags)
        to_add = sorted(current_set - self._original_tags)
        to_remove = sorted(self._original_tags - current_set)
        if to_add or to_remove:
            logging.debug(f"TagEditor: add={to_add}, remove={to_remove}")
            self.tags_confirmed.emit(to_add, to_remove)
            self._original_tags = current_set

    def _on_enter_confirmed(self):
        self._on_confirm(self.tag_input.get_tags())
        self.close()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
