from typing import Optional

from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QPushButton, QHBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal, QSettings
from PySide6.QtGui import QFont

from .content_provider import ContentProvider
from ..components.collapsible_section import CollapsibleSection

# Palette — mirrors HotkeyHelpOverlay
_BG = "#1e1e1e"
_BG_TOOLBAR = "#181818"
_TEXT = "#dcdcdc"
_PIN_BG = "#464646"
_PIN_BG_ACTIVE = "#8cb4ff"
_PIN_FG = "#dcdcdc"
_PIN_FG_ACTIVE = "#1e1e1e"
_BORDER = "#2a2a2a"
_FONT = "monospace"
_FONT_SIZE = 12


class InfoPanelShell(QWidget):
    """Top-level window displaying structured info for the hovered/pinned image."""

    closed = Signal()

    def __init__(self, provider: ContentProvider, metadata_cache,
                 panel_index: int = 0, config_manager=None):
        super().__init__(None, Qt.Window)
        self._provider = provider
        self._metadata_cache = metadata_cache
        self._panel_index = panel_index
        self._config_manager = config_manager

        self._pinned = False
        self._pinned_path: Optional[str] = None
        self._current_path: Optional[str] = None
        self._sections: dict[str, CollapsibleSection] = {}
        self._collapsed_state: dict[str, bool] = {}

        self.setWindowTitle(f"Info: {provider.provider_name}")
        self.setMinimumSize(280, 200)

        # Restore geometry
        settings = QSettings("RabbitViewer", "InfoPanel")
        geometry = settings.value(f"geometry_{self._panel_index}")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(320, 500)

        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet(f"""
            InfoPanelShell {{
                background: {_BG};
                border: 1px solid {_BORDER};
            }}
        """)

        font = QFont(_FONT, _FONT_SIZE)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setStyleSheet(f"""
            background: {_BG_TOOLBAR};
            border-bottom: 1px solid {_BORDER};
        """)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 6, 10, 6)

        self._path_label = QLabel("")
        self._path_label.setStyleSheet(f"""
            color: {_TEXT};
            font-family: {_FONT};
            font-size: {_FONT_SIZE}px;
            font-weight: bold;
        """)
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        toolbar_layout.addWidget(self._path_label, 1)

        self._pin_button = QPushButton("Pin")
        self._pin_button.setCheckable(True)
        self._pin_button.setFixedHeight(22)
        self._pin_button.setToolTip("Pin to current image")
        self._pin_button.setStyleSheet(f"""
            QPushButton {{
                background: {_PIN_BG};
                color: {_PIN_FG};
                border: none;
                border-radius: 4px;
                padding: 2px 10px;
                font-family: {_FONT};
                font-size: {_FONT_SIZE - 1}px;
            }}
            QPushButton:checked {{
                background: {_PIN_BG_ACTIVE};
                color: {_PIN_FG_ACTIVE};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: #555;
            }}
            QPushButton:checked:hover {{
                background: #7da4e8;
            }}
        """)
        self._pin_button.toggled.connect(self._on_pin_toggled)
        toolbar_layout.addWidget(self._pin_button)

        main_layout.addWidget(toolbar)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {_BG};
                border: none;
            }}
            QScrollBar:vertical {{
                background: {_BG};
                width: 6px;
            }}
            QScrollBar::handle:vertical {{
                background: {_PIN_BG};
                border-radius: 3px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet(f"background: {_BG};")
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 4, 0, 0)
        self._scroll_layout.setSpacing(1)
        self._scroll_layout.addStretch()
        self._scroll.setWidget(self._scroll_content)
        main_layout.addWidget(self._scroll)

    # -- Public API (called by MainWindow) --

    def on_thumbnail_hovered(self, path: str):
        if self._pinned:
            return
        self._update_for_path(path)

    def on_thumbnail_left(self):
        if self._pinned:
            return
        # Keep last display — don't clear on leave

    def refresh_if_showing(self, path: str):
        """Re-render sections if currently displaying this path."""
        if path == self._current_path:
            self._refresh_sections()

    # -- Internal --

    def _update_for_path(self, path: str):
        if not path or path == self._current_path:
            return
        self._current_path = path
        self._path_label.setText(path.rsplit("/", 1)[-1])
        self._refresh_sections()

    def _refresh_sections(self):
        if not self._current_path:
            return

        sections = self._provider.get_sections(self._current_path)
        new_titles = {s.title for s in sections}

        # Remove stale
        for title in list(self._sections.keys()):
            if title not in new_titles:
                widget = self._sections.pop(title)
                self._scroll_layout.removeWidget(widget)
                widget.deleteLater()

        # Update or create
        insert_idx = 0
        for section_data in sections:
            if section_data.title in self._sections:
                self._sections[section_data.title].set_rows(section_data.rows)
            else:
                widget = CollapsibleSection(section_data.title)
                widget.set_rows(section_data.rows)
                if section_data.title in self._collapsed_state:
                    widget.set_collapsed(self._collapsed_state[section_data.title])
                widget.toggled.connect(
                    lambda collapsed, t=section_data.title:
                        self._collapsed_state.__setitem__(t, collapsed)
                )
                self._sections[section_data.title] = widget
                self._scroll_layout.insertWidget(insert_idx, widget)
            insert_idx += 1

    def _on_pin_toggled(self, checked: bool):
        self._pinned = checked
        if checked:
            self._pinned_path = self._current_path
            self._pin_button.setText("Unpin")
        else:
            self._pinned_path = None
            self._pin_button.setText("Pin")
        self._update_window_title()

    def _update_window_title(self):
        base = f"Info: {self._provider.provider_name}"
        if self._pinned:
            self.setWindowTitle(f"{base} (Pinned)")
        else:
            self.setWindowTitle(base)

    def closeEvent(self, event):
        settings = QSettings("RabbitViewer", "InfoPanel")
        settings.setValue(f"geometry_{self._panel_index}", self.saveGeometry())
        settings.sync()
        self._provider.on_cleanup()
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()
