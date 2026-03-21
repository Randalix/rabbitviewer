from PySide6.QtWidgets import QPushButton, QApplication
from PySide6.QtCore import Qt, Signal
import logging
logger = logging.getLogger(__name__)
from gui.components.star_drag_context import StarDragContext


class StarButton(QPushButton):
    toggled = Signal(int, bool)

    def __init__(self, index: int, initial_state: bool = True, parent=None,
                 drag_context: StarDragContext = None):
        super().__init__(parent)
        self.index = index
        self._current_state = initial_state
        self._drag = drag_context or StarDragContext()
        self._siblings: list["StarButton"] | None = None  # cached on first drag; valid for fixed-layout rows
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover)
        self.update_style()

    def set_state(self, state: bool):
        if self._current_state != state:
            self._current_state = state
            self.update_style()
            self.toggled.emit(self.index, self._current_state)

    def set_state_silent(self, state: bool):
        """Update state and appearance without emitting toggled — use for programmatic sync."""
        if self._current_state != state:
            self._current_state = state
            self.update_style()

    def get_state(self) -> bool:
        return self._current_state

    def update_style(self):
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
        if event.button() == Qt.LeftButton:
            self._drag.is_active = True
            self._drag.initial_state = not self._current_state
            self._drag.last_button = self
            self.set_state(self._drag.initial_state)
            # Cache siblings once per drag so mouseMoveEvent doesn't re-scan on every pixel.
            # Only valid for fixed-layout rows where the parent contains exactly these buttons.
            self._siblings = [s for s in self.parent().findChildren(StarButton) if s is not self] if self.parent() else []
            event.accept()
            logger.debug(f"Mouse press on button {self.index}, drag state: {self._drag.initial_state}")
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag.is_active and event.buttons() & Qt.LeftButton:
            global_pos = self.mapToGlobal(event.pos())
            if self.geometry().contains(self.parent().mapFromGlobal(global_pos)):
                if self._drag.last_button != self:
                    self.set_state(self._drag.initial_state)
                    self._drag.last_button = self
                    logger.debug(f"Dragged to button {self.index}, set state to {self._drag.initial_state}")
            self._check_drag_on_siblings(global_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def _check_drag_on_siblings(self, global_pos):
        if self._siblings is None:
            return
        for sibling in self._siblings:
            if sibling != self._drag.last_button:
                local_pos = sibling.mapFromGlobal(global_pos)
                if sibling.rect().contains(local_pos):
                    sibling.set_state(self._drag.initial_state)
                    self._drag.last_button = sibling
                    logger.debug(f"Dragged to sibling button {sibling.index}, set state to {self._drag.initial_state}")
                    break

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            logger.debug(f"Mouse release, ending drag operation")
            self._drag.is_active = False
            self._drag.last_button = None
            self._siblings = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        if self._drag.is_active:
            mouse_buttons = QApplication.mouseButtons()
            if mouse_buttons & Qt.LeftButton:
                if self._drag.last_button != self:
                    self.set_state(self._drag.initial_state)
                    self._drag.last_button = self
                    logger.debug(f"Entered button {self.index} during drag, set state to {self._drag.initial_state}")
        super().enterEvent(event)

    def leaveEvent(self, event):
        super().leaveEvent(event)
