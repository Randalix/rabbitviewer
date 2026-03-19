from PySide6.QtWidgets import QDialog, QVBoxLayout, QLineEdit, QLabel
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
import time

from core.event_system import event_system, EventType, TextFilterEventData


class NameFilterDialog(QDialog):
    """Filter images by filename."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by Name")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.resize(320, 80)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._emit)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Filter by name:"))
        self._input = QLineEdit()
        self._input.setPlaceholderText("Enter filename...")
        self._input.textEdited.connect(self._debounce.start)
        layout.addWidget(self._input)

        QShortcut(QKeySequence("Esc"), self).activated.connect(self._close_and_clear)

    def _emit(self):
        event_system.publish(TextFilterEventData(
            event_type=EventType.TEXT_FILTER_CHANGED, source="name_filter_dialog",
            timestamp=time.time(), filter_text=self._input.text()))

    def showEvent(self, event):
        super().showEvent(event)
        self._input.setFocus()
        self._input.selectAll()

    def hideEvent(self, event):
        if self._debounce.isActive():
            self._debounce.stop()
            self._emit()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._debounce.stop()
            self._emit()
            self.hide()
            return
        super().keyPressEvent(event)

    def _close_and_clear(self):
        self.clear_filter()
        self.close()

    def clear_filter(self):
        self._input.clear()
        self._emit()
