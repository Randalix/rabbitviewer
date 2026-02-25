import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, List

from PySide6.QtWidgets import QWidget, QGraphicsDropShadowEffect, QApplication
from PySide6.QtCore import Qt, QSize, QEvent, QObject
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath, QKeyEvent, QMouseEvent


@dataclass
class MenuContext:
    view: str  # "thumbnail" | "picture" | "video"
    has_selection: bool
    selection_count: int
    file_types: set


@dataclass
class MenuNode:
    label: str
    key: str = ""
    script: str = ""
    children: list = field(default_factory=list)
    visible: Optional[Callable] = None


class ModalMenu(QWidget):
    """Floating overlay menu activated by a trigger key.

    Each visible MenuNode is rendered as a row ``[K]  Label``.  Pressing a
    key either runs the associated script (leaf) or descends into a sub-menu
    (branch).  Escape or any unmapped key dismisses the menu.

    Key interception uses a QApplication eventFilter rather than Qt.Popup +
    keyPressEvent.  This runs before QShortcut matching and before Qt's popup
    auto-dismiss logic, so normal hotkeys cannot leak through while the menu
    is open.
    """

    _PADDING = 24
    _ROW_HEIGHT = 32
    _KEY_WIDTH = 36
    _FONT_SIZE = 13
    _BG_COLOR = QColor(30, 30, 30, 230)
    _KEY_BG = QColor(70, 70, 70)
    _KEY_FG = QColor(220, 200, 100)
    _LABEL_FG = QColor(220, 220, 220)
    _HEADER_FG = QColor(160, 160, 160)
    _CORNER_RADIUS = 10

    def __init__(self, parent, menus: dict, script_manager):
        super().__init__(parent)
        self._menus = menus
        self._script_manager = script_manager
        self._is_open = False

        self._breadcrumb: List[str] = []
        self._current_node: Optional[MenuNode] = None
        self._visible_items: List[MenuNode] = []
        self._key_map: dict = {}
        self._context: Optional[MenuContext] = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(self, menu_id: str):
        ctx = self._build_context()
        root = self._menus.get(menu_id)
        if not root:
            logging.warning(f"ModalMenu: unknown menu '{menu_id}'")
            return

        self._context = ctx
        self._breadcrumb = [root.label]
        self._show_node(root)

    # ------------------------------------------------------------------
    # Internal navigation
    # ------------------------------------------------------------------

    def _show_node(self, node: MenuNode):
        self._current_node = node
        self._visible_items = [
            child for child in node.children
            if child.visible is None or child.visible(self._context)
        ]
        self._key_map = {item.key.lower(): item for item in self._visible_items if item.key}
        logging.debug(f"ModalMenu._show_node: {node.label}, {len(self._visible_items)} visible, keys={list(self._key_map.keys())}")

        if not self._visible_items:
            self._close()
            return

        self._resize_to_fit()
        self._position_center()

        if not self._is_open:
            self._is_open = True
            # why: eventFilter installed on QApplication intercepts keys before
            # QShortcut matching and before HotkeyManager's own eventFilter,
            # because Qt calls filters in reverse installation order (LIFO).
            app = QApplication.instance()
            if app:
                app.installEventFilter(self)
        self.show()
        self.raise_()
        self.update()

    def _resize_to_fit(self):
        rows = len(self._visible_items)
        header_height = self._ROW_HEIGHT if len(self._breadcrumb) > 1 else 0
        h = self._PADDING * 2 + header_height + rows * self._ROW_HEIGHT
        max_label = max((len(item.label) for item in self._visible_items), default=6)
        w = self._PADDING * 2 + self._KEY_WIDTH + 12 + max_label * (self._FONT_SIZE * 0.65)
        w = max(int(w), 180)
        self.setFixedSize(QSize(w, h))

    def _position_center(self):
        # why: Tool windows use global screen coordinates
        parent = self.parentWidget()
        if parent:
            px = parent.width() // 2 - self.width() // 2
            py = parent.height() // 2 - self.height() // 2
            self.move(parent.mapToGlobal(parent.rect().topLeft()).x() + px,
                      parent.mapToGlobal(parent.rect().topLeft()).y() + py)

    # ------------------------------------------------------------------
    # Event filter — intercepts keys/clicks at the QApplication level
    # ------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if not self._is_open:
            return False

        # why: QShortcut matching happens during ShortcutOverride, before KeyPress.
        # Consuming ShortcutOverride prevents Qt from firing any QShortcut while
        # the menu is open, so HotkeyManager shortcuts cannot leak through.
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True

        if event.type() == QEvent.Type.KeyPress:
            self._handle_key(event)
            return True  # consume ALL key events while open

        if event.type() == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent):
                global_pos = event.globalPosition().toPoint()
                if not self.geometry().contains(global_pos):
                    logging.debug("ModalMenu: click outside, dismissing")
                    self._close()
                    return True

        return False

    def _handle_key(self, event: QKeyEvent):
        logging.debug(f"ModalMenu._handle_key: key={event.key()}, text='{event.text()}'")
        if event.key() == Qt.Key_Escape:
            self._close()
            return

        text = event.text().lower()
        item = self._key_map.get(text)
        if item:
            if item.children:
                self._breadcrumb.append(item.label)
                self._show_node(item)
            elif item.script:
                logging.debug(f"ModalMenu: running script '{item.script}'")
                self._close()
                self._script_manager.run_script(item.script)
        else:
            logging.debug(f"ModalMenu: unmapped key '{text}', dismissing")
            self._close()

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self) -> MenuContext:
        mw = self.parentWidget()
        current = mw.stacked_widget.currentWidget()
        if current is mw.picture_view:
            view = "picture"
        elif mw.video_view and current is mw.video_view:
            view = "video"
        else:
            view = "thumbnail"

        selected = set()
        if hasattr(mw, "selection_state"):
            selected = mw.selection_state.selected_paths

        file_types = set()
        paths_for_types = selected
        if not paths_for_types and hasattr(mw, "current_hovered_image") and mw.current_hovered_image:
            paths_for_types = {mw.current_hovered_image}
        for p in paths_for_types:
            _, ext = os.path.splitext(p)
            if ext:
                file_types.add(ext.lower())

        return MenuContext(
            view=view,
            has_selection=bool(selected),
            selection_count=len(selected),
            file_types=file_types,
        )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _close(self):
        if not self._is_open:
            return
        logging.debug("ModalMenu._close")
        self._is_open = False
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)
        self.hide()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(self.rect().toRectF(), self._CORNER_RADIUS, self._CORNER_RADIUS)
        painter.fillPath(path, self._BG_COLOR)

        font = QFont("monospace", self._FONT_SIZE)
        font.setStyleHint(QFont.Monospace)
        painter.setFont(font)

        y = self._PADDING

        if len(self._breadcrumb) > 1:
            painter.setPen(self._HEADER_FG)
            crumb = " > ".join(self._breadcrumb)
            painter.drawText(self._PADDING, y, self.width(), self._ROW_HEIGHT,
                             Qt.AlignVCenter | Qt.AlignLeft, crumb)
            y += self._ROW_HEIGHT

        for item in self._visible_items:
            key_x = self._PADDING
            key_rect = painter.boundingRect(key_x, y, self._KEY_WIDTH, self._ROW_HEIGHT,
                                            Qt.AlignCenter, item.key.upper())
            badge_rect = key_rect.adjusted(-6, -2, 6, 2)
            badge_path = QPainterPath()
            badge_path.addRoundedRect(badge_rect, 4, 4)
            painter.fillPath(badge_path, self._KEY_BG)

            painter.setPen(self._KEY_FG)
            painter.drawText(key_x, y, self._KEY_WIDTH, self._ROW_HEIGHT,
                             Qt.AlignCenter, item.key.upper())

            painter.setPen(self._LABEL_FG)
            label_x = key_x + self._KEY_WIDTH + 12
            suffix = "  >" if item.children else ""
            painter.drawText(label_x, y, self.width() - label_x - self._PADDING,
                             self._ROW_HEIGHT, Qt.AlignVCenter | Qt.AlignLeft,
                             item.label + suffix)

            y += self._ROW_HEIGHT

        painter.end()
