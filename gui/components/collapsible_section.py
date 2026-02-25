from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, Signal

# Palette — mirrors HotkeyHelpOverlay
_BG = "#1e1e1e"
_HEADER_BG = "#232323"
_HEADER_FG = "#8cb4ff"       # section header — blue accent
_ARROW_FG = "#8cb4ff"
_KEY_FG = "#8c8c8c"          # row key — dim
_VAL_FG = "#dcdcdc"          # row value — bright
_SEPARATOR = "#2a2a2a"
_FONT = "monospace"
_FONT_SIZE = 12


class CollapsibleSection(QWidget):
    """A section with a clickable header that toggles body visibility."""

    toggled = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self.setStyleSheet(f"background: {_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        self._header = QFrame()
        self._header.setStyleSheet(f"""
            QFrame {{
                background: {_HEADER_BG};
                border-bottom: 1px solid {_SEPARATOR};
            }}
        """)
        self._header.setCursor(Qt.PointingHandCursor)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 4, 10, 4)

        self._arrow = QLabel("\u25bc")
        self._arrow.setFixedWidth(14)
        self._arrow.setStyleSheet(f"""
            color: {_ARROW_FG};
            font-family: {_FONT};
            font-size: {_FONT_SIZE - 2}px;
        """)
        self._title_label = QLabel(title.upper())
        self._title_label.setStyleSheet(f"""
            color: {_HEADER_FG};
            font-family: {_FONT};
            font-size: {_FONT_SIZE - 1}px;
            font-weight: bold;
            letter-spacing: 1px;
        """)
        header_layout.addWidget(self._arrow)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        layout.addWidget(self._header)

        # Body
        self._body = QWidget()
        self._body.setStyleSheet(f"background: {_BG};")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(10, 6, 10, 6)
        self._body_layout.setSpacing(1)
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
            row_widget.setStyleSheet(f"background: {_BG};")
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 1, 0, 1)
            row_layout.setSpacing(8)

            key_label = QLabel(f"{key}" if key else "")
            key_label.setStyleSheet(f"""
                color: {_KEY_FG};
                font-family: {_FONT};
                font-size: {_FONT_SIZE}px;
                min-width: 90px;
                max-width: 90px;
            """)
            key_label.setAlignment(Qt.AlignTop | Qt.AlignRight)

            val_label = QLabel(str(value))
            val_label.setStyleSheet(f"""
                color: {_VAL_FG};
                font-family: {_FONT};
                font-size: {_FONT_SIZE}px;
            """)
            val_label.setWordWrap(True)
            val_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

            row_layout.addWidget(key_label)
            row_layout.addWidget(val_label, 1)
            self._body_layout.addWidget(row_widget)

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed
