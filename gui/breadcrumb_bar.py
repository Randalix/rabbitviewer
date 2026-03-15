"""On-demand breadcrumb path bar triggered by Ctrl+L.

Shows the current directory path as clickable segments. Arrow buttons
between segments open dropdown menus listing sibling folders with image
counts.  Selecting a segment or sibling navigates into that folder.
"""
import os
import logging
import threading

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QPushButton, QToolButton, QMenu, QSizePolicy,
    QScrollArea, QWidget,
)

from core.event_system import event_system, EventType, DirectoryEventData

logger = logging.getLogger(__name__)


def _navigate(folder_path: str):
    """Publish a folder navigation event via EventSystem."""
    import time
    event_system.publish(DirectoryEventData(
        event_type=EventType.NAVIGATE_TO_FOLDER,
        source="breadcrumb_bar",
        timestamp=time.time(),
        path=folder_path,
    ))


class BreadcrumbBar(QDialog):
    """Pop-up breadcrumb bar for directory navigation."""

    _siblings_ready = Signal(str, QToolButton, list)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Navigate")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self._current_path: str = ""
        self._segments: list = []
        self._pending_sibling_path: str = ""

        self._setup_ui()
        self._setup_shortcuts()
        self._siblings_ready.connect(self._on_siblings_ready)

    def _setup_ui(self):
        self.setStyleSheet("""
            QDialog {
                background: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
            }
        """)

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(6, 0, 6, 0)
        outer_layout.setSpacing(0)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setFrameShape(QScrollArea.NoFrame)
        self._scroll_area.setStyleSheet("QScrollArea { background: transparent; }")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._bar_layout = QHBoxLayout(self._container)
        self._bar_layout.setContentsMargins(0, 0, 0, 0)
        self._bar_layout.setSpacing(0)

        self._scroll_area.setWidget(self._container)
        outer_layout.addWidget(self._scroll_area)

        self.setFixedHeight(28)

    def _setup_shortcuts(self):
        for key in ("Esc", "Ctrl+L"):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(self.hide)

    def set_path(self, path: str):
        """Rebuild the breadcrumb segments for the given directory path."""
        if not path:
            return
        self._current_path = path

        current = os.path.normpath(path)
        parts = []
        while True:
            head, tail = os.path.split(current)
            if tail:
                parts.append((tail, current))
            elif head:
                parts.append((head, head))
                break
            else:
                break
            current = head
        parts.reverse()
        self._segments = parts

        self._rebuild_buttons()

    def _rebuild_buttons(self):
        """Clear and rebuild the segment + arrow buttons."""
        while self._bar_layout.count():
            item = self._bar_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        for i, (name, full_path) in enumerate(self._segments):
            is_last = (i == len(self._segments) - 1)

            # Separator arrow before each segment (except the first)
            if i > 0:
                arrow = QToolButton()
                arrow.setText("\u203a")  # ›
                arrow.setCursor(Qt.PointingHandCursor)
                arrow.setStyleSheet("""
                    QToolButton {
                        color: #555; background: transparent; border: none;
                        padding: 0 1px; font-size: 13px; min-width: 12px;
                    }
                    QToolButton:hover { color: #ccc; }
                """)
                # Arrow navigates to siblings of the segment AFTER it
                arrow.clicked.connect(
                    lambda checked, p=full_path, a=arrow: self._on_arrow_clicked(p, a))
                self._bar_layout.addWidget(arrow)

            btn = QPushButton(name)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            if is_last:
                btn.setStyleSheet("""
                    QPushButton {
                        color: #e0e0e0; background: transparent; border: none;
                        padding: 2px 4px; font-size: 12px; font-weight: bold;
                    }
                    QPushButton:hover { color: #fff; }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        color: #888; background: transparent; border: none;
                        padding: 2px 4px; font-size: 12px;
                    }
                    QPushButton:hover { color: #ccc; }
                """)
            btn.clicked.connect(lambda checked, p=full_path: self._on_segment_clicked(p))
            self._bar_layout.addWidget(btn)

        # Trailing arrow for subdirectories of the current folder
        if self._segments:
            current_path = self._segments[-1][1]
            trail = QToolButton()
            trail.setText("\u203a")
            trail.setCursor(Qt.PointingHandCursor)
            trail.setStyleSheet("""
                QToolButton {
                    color: #555; background: transparent; border: none;
                    padding: 0 1px; font-size: 13px; min-width: 12px;
                }
                QToolButton:hover { color: #ccc; }
            """)
            trail.clicked.connect(
                lambda checked, p=current_path, a=trail: self._on_subdirs_clicked(p, a))
            self._bar_layout.addWidget(trail)

        QTimer.singleShot(0, self._scroll_to_end)

    def _scroll_to_end(self):
        sb = self._scroll_area.horizontalScrollBar()
        sb.setValue(sb.maximum())

    def _on_segment_clicked(self, path: str):
        if path == self._current_path:
            return
        self.hide()
        _navigate(path)

    def _on_subdirs_clicked(self, current_path: str, arrow_button: QToolButton):
        """Fetch subdirectories of the current folder."""
        self._pending_sibling_path = current_path + "::subdirs"
        threading.Thread(
            target=self._fetch_subdirs,
            args=(current_path, arrow_button),
            daemon=True,
            name="breadcrumb-subdirs",
        ).start()

    def _fetch_subdirs(self, current_path: str, arrow_button: QToolButton):
        try:
            nodes = self.service.get_subdirectories(current_path)
            self._siblings_ready.emit(current_path + "::subdirs", arrow_button, nodes)
        except Exception as e:
            logger.error("Failed to fetch subdirs for %s: %s", current_path, e)

    def _on_arrow_clicked(self, segment_path: str, arrow_button: QToolButton):
        self._pending_sibling_path = segment_path
        threading.Thread(
            target=self._fetch_siblings,
            args=(segment_path, arrow_button),
            daemon=True,
            name="breadcrumb-siblings",
        ).start()

    def _fetch_siblings(self, segment_path: str, arrow_button: QToolButton):
        try:
            parent_of = os.path.dirname(segment_path)
            nodes = self.service.get_subdirectories(parent_of) if parent_of else []
            self._siblings_ready.emit(segment_path, arrow_button, nodes)
        except Exception as e:
            logger.error("Failed to fetch siblings for %s: %s", segment_path, e)

    @Slot(str, QToolButton, list)
    def _on_siblings_ready(self, segment_path: str, arrow_button: QToolButton, siblings: list):
        if segment_path != self._pending_sibling_path:
            return
        if not siblings:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: #252526; color: #ccc;
                border: 1px solid #3c3c3c; padding: 2px;
                font-size: 12px;
            }
            QMenu::item { padding: 3px 20px 3px 8px; }
            QMenu::item:selected { background: #094771; color: #fff; }
            QMenu::separator { height: 1px; background: #3c3c3c; margin: 2px 4px; }
        """)

        for node in siblings:
            count = node.recursive_count or node.image_count
            label = f"{node.name}   {count}" if count else node.name
            action = menu.addAction(label)
            if node.path == segment_path:
                action.setEnabled(False)
                font = action.font()
                font.setBold(True)
                action.setFont(font)
            action.triggered.connect(lambda checked, p=node.path: self._on_sibling_selected(p))

        menu.exec(arrow_button.mapToGlobal(arrow_button.rect().bottomLeft()))

    def _on_sibling_selected(self, path: str):
        self.hide()
        _navigate(path)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if parent:
            pw = parent.width()
            bar_width = min(pw - 40, 900)
            self.setFixedWidth(bar_width)
            x = parent.mapToGlobal(parent.rect().topLeft()).x() + (pw - bar_width) // 2
            y = parent.mapToGlobal(parent.rect().topLeft()).y() + 4
            self.move(x, y)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.hide()
            return
        super().keyPressEvent(event)
