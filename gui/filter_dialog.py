from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
import logging

from gui.components.star_button import StarButton, StarDragContext


class FilterDialog(QDialog):
    """Pop-up filter dialog for image search."""

    filter_changed = Signal(str)  # Signal emitted when the filter text changes
    stars_changed = Signal(list)  # Signal emitted when the star selection changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter")
        self.setModal(False)  # Non-modal to allow interaction with the main window
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(400, 200)

        # Debounce timer for text input
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(300) # 300 ms delay
        self.debounce_timer.timeout.connect(self._emit_filter_text)

        # Star button states (all active by default)
        # Index 0: 0 stars (no rating), Index 1: 1 star, ..., Index 5: 5 stars
        self.star_states = [True, True, True, True, True, True]

        self.setup_ui()
        self.setup_shortcuts()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        label = QLabel("Filter:")
        layout.addWidget(label)

        star_layout = QHBoxLayout()
        self.star_buttons = []
        self._star_drag_ctx = StarDragContext()

        for i in range(6):
            button = StarButton(index=i, initial_state=self.star_states[i],
                                drag_context=self._star_drag_ctx)
            button.setFixedSize(60, 30)
            button.toggled.connect(self._on_star_button_toggled)
            self.star_buttons.append(button)
            star_layout.addWidget(button)

        layout.addLayout(star_layout)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Enter search term...")
        self.filter_input.textEdited.connect(self.on_filter_changed)
        layout.addWidget(self.filter_input)

        self.filter_input.setFocus()

    def setup_shortcuts(self):
        escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        escape_shortcut.activated.connect(self.close)

    def on_filter_changed(self, text):
        self.debounce_timer.start()

    def _emit_filter_text(self):
        self.filter_changed.emit(self.filter_input.text())

    def showEvent(self, event):
        super().showEvent(event)
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def clear_filter(self):
        self.filter_input.clear()
        for button in self.star_buttons:
            button.set_state(True)

    def _on_star_button_toggled(self, index: int, new_state: bool):
        # Qt signal delivers int; cast to bool for list consistency
        state_as_bool = bool(new_state)
        logging.debug(f"Handler received: index={index}, new_state={new_state} (bool: {state_as_bool}). States BEFORE: {self.star_states}")
        if 0 <= index < len(self.star_states):
            self.star_states[index] = state_as_bool
            self.stars_changed.emit(list(self.star_states))
            logging.debug(f"Star button {index} toggled. States AFTER: {self.star_states}")
        else:
            logging.error(f"Invalid index {index} received in _on_star_button_toggled")

    def hideEvent(self, event):
        if self.debounce_timer.isActive():
            self.debounce_timer.stop()
            self._emit_filter_text()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.filter_changed.emit(self.filter_input.text())
            self.hide()
            return
        super().keyPressEvent(event)
