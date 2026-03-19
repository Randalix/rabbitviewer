from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
import time

from core.event_system import event_system, EventType, StarFilterEventData
from gui.components.star_button import StarButton, StarDragContext


class RatingFilterDialog(QDialog):
    """Filter by star rating."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by Rating")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)

        self.star_states = [True] * 6

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Show ratings:"))

        star_layout = QHBoxLayout()
        self.star_buttons = []
        self._star_drag_ctx = StarDragContext()
        for i in range(6):
            btn = StarButton(index=i, initial_state=True, drag_context=self._star_drag_ctx)
            btn.setFixedSize(60, 30)
            btn.toggled.connect(self._on_toggled)
            self.star_buttons.append(btn)
            star_layout.addWidget(btn)
        layout.addLayout(star_layout)

        self.adjustSize()

        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

    def _on_toggled(self, index: int, new_state: bool):
        if 0 <= index < len(self.star_states):
            self.star_states[index] = bool(new_state)
            event_system.publish(StarFilterEventData(
                event_type=EventType.STAR_FILTER_CHANGED, source="rating_filter_dialog",
                timestamp=time.time(), star_states=list(self.star_states)))

    def clear_filter(self):
        self.star_states = [True] * 6
        for btn in self.star_buttons:
            btn.set_state(True)
