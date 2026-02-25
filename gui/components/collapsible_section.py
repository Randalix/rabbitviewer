from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, Signal


class CollapsibleSection(QWidget):
    """A section with a clickable header that toggles body visibility."""

    toggled = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._collapsed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        self._header = QFrame()
        self._header.setStyleSheet("QFrame { background: #2a2a2a; padding: 4px; }")
        self._header.setCursor(Qt.PointingHandCursor)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(6, 2, 6, 2)

        self._arrow = QLabel("\u25bc")
        self._arrow.setFixedWidth(14)
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("font-weight: bold; color: #ccc;")
        header_layout.addWidget(self._arrow)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        layout.addWidget(self._header)

        # Body
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(12, 4, 6, 4)
        self._body_layout.setSpacing(2)
        layout.addWidget(self._body)

        self._header.mousePressEvent = self._on_header_clicked

    def _on_header_clicked(self, event):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._arrow.setText("\u25b6" if collapsed else "\u25bc")
        self.toggled.emit(collapsed)

    def set_rows(self, rows: list):
        """Replace body content with [(key, value), ...] pairs."""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for key, value in rows:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            key_label = QLabel(f"{key}:" if key else "")
            key_label.setStyleSheet("color: #888; min-width: 100px;")
            key_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            val_label = QLabel(str(value))
            val_label.setStyleSheet("color: #ddd;")
            val_label.setWordWrap(True)
            val_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

            row_layout.addWidget(key_label)
            row_layout.addWidget(val_label, 1)
            self._body_layout.addWidget(row_widget)

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed
