from PySide6.QtWidgets import QPushButton, QApplication
from PySide6.QtCore import Qt, Signal, QPoint
import logging
from gui.components.star_drag_context import StarDragContext


class StarButton(QPushButton):
    """
    A custom QPushButton that supports toggling its state by clicking
    and dragging over adjacent buttons.
    """

    # Signal emitted when the button's state is toggled
    # Parameters: index (int), new_state (bool)
    toggled = Signal(int, bool)

    def __init__(self, index: int, initial_state: bool = True, parent=None,
                 drag_context: StarDragContext = None):
        super().__init__(parent)
        self.index = index  # The index of the button (0 for 0 stars, 1 for 1 star, etc.)
        self._current_state = initial_state
        self._drag = drag_context or StarDragContext()
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover)
        self.update_style()

    def set_state(self, state: bool):
        """Sets the state of the button and updates its appearance."""
        if self._current_state != state:
            self._current_state = state
            self.update_style()
            self.toggled.emit(self.index, self._current_state)

    def get_state(self) -> bool:
        """Returns the current state of the button."""
        return self._current_state

    def update_style(self):
        """Updates the visual appearance of the button based on its state."""
        if self.index == 0:
            self.setText("-")
        else:
            stars = "★" * self.index
            self.setText(stars)

        if self._current_state:
            self.setStyleSheet("QPushButton { color: orange; font-size: 14px; border: none; background: transparent; }")
        else:
            self.setStyleSheet("QPushButton { color: gray; font-size: 14px; border: none; background: transparent; }")

    def mousePressEvent(self, event):
        """Handles mouse click events."""
        if event.button() == Qt.LeftButton:
            self._drag.is_active = True
            self._drag.initial_state = not self._current_state
            self._drag.last_button = self
            self.set_state(self._drag.initial_state)
            event.accept()
            logging.debug(f"Mouse press on button {self.index}, drag state: {self._drag.initial_state}")
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handles mouse move events."""
        if self._drag.is_active and event.buttons() & Qt.LeftButton:
            global_pos = self.mapToGlobal(event.pos())
            if self.geometry().contains(self.parent().mapFromGlobal(global_pos)):
                if self._drag.last_button != self:
                    self.set_state(self._drag.initial_state)
                    self._drag.last_button = self
                    logging.debug(f"Dragged to button {self.index}, set state to {self._drag.initial_state}")
            self._check_drag_on_siblings(global_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def _check_drag_on_siblings(self, global_pos):
        """Check if drag operation should affect sibling buttons."""
        if not self.parent():
            return
        for sibling in self.parent().findChildren(StarButton):
            if sibling != self and sibling != self._drag.last_button:
                local_pos = sibling.mapFromGlobal(global_pos)
                if sibling.rect().contains(local_pos):
                    sibling.set_state(self._drag.initial_state)
                    self._drag.last_button = sibling
                    logging.debug(f"Dragged to sibling button {sibling.index}, set state to {self._drag.initial_state}")
                    break

    def mouseReleaseEvent(self, event):
        """Handles mouse release events."""
        if event.button() == Qt.LeftButton:
            logging.debug(f"Mouse release, ending drag operation")
            self._drag.is_active = False
            self._drag.last_button = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        """Handles the mouse pointer entering the button area."""
        if self._drag.is_active:
            mouse_buttons = QApplication.mouseButtons()
            if mouse_buttons & Qt.LeftButton:
                if self._drag.last_button != self:
                    self.set_state(self._drag.initial_state)
                    self._drag.last_button = self
                    logging.debug(f"Entered button {self.index} during drag, set state to {self._drag.initial_state}")
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Handles the mouse pointer leaving the button area."""
        super().leaveEvent(event)
