from typing import Optional

from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QPushButton, QHBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal, QSettings

from core.event_system import event_system, EventType
from .content_provider import ContentProvider
from ..components.collapsible_section import CollapsibleSection


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

        event_system.subscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setStyleSheet("background: #1e1e1e;")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(6, 4, 6, 4)

        self._path_label = QLabel("")
        self._path_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        toolbar_layout.addWidget(self._path_label, 1)

        self._pin_button = QPushButton("Pin")
        self._pin_button.setCheckable(True)
        self._pin_button.setFixedHeight(24)
        self._pin_button.setToolTip("Pin to current image")
        self._pin_button.toggled.connect(self._on_pin_toggled)
        toolbar_layout.addWidget(self._pin_button)

        main_layout.addWidget(toolbar)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
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

    def _on_daemon_notification(self, event_data):
        pass

    def closeEvent(self, event):
        settings = QSettings("RabbitViewer", "InfoPanel")
        settings.setValue(f"geometry_{self._panel_index}", self.saveGeometry())
        settings.sync()
        event_system.unsubscribe(EventType.DAEMON_NOTIFICATION, self._on_daemon_notification)
        self._provider.on_cleanup()
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()
