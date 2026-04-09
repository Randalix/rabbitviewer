# gui/folder_tree_panel.py
"""Left-side folder tree panel with lazy loading and type-to-search."""
from __future__ import annotations

import os
import logging

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor, QPainter, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLineEdit,
    QMenu,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.bookmark_manager import add_bookmark, load_bookmarks, remove_bookmark

logger = logging.getLogger(__name__)

_PLACEHOLDER = "__placeholder__"


def _match_score(name: str, query: str) -> int:
    """Return a match score for *name* against *query* (higher is better, 0 = no match).

    3 — exact (case-insensitive)
    2 — name starts with query
    1 — query is a substring of name
    """
    lower = name.lower()
    if lower == query:
        return 3
    if lower.startswith(query):
        return 2
    if query in lower:
        return 1
    return 0


class _HighlightDelegate(QStyledItemDelegate):
    """Paints a yellow background behind the matched substring."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""

    def set_query(self, query: str) -> None:
        self._query = query.lower()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        # Let the base class draw selection background, text, etc.
        super().paint(painter, option, index)

        if not self._query:
            return

        text = index.data(Qt.DisplayRole) or ""
        pos = text.lower().find(self._query)
        if pos < 0:
            return

        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        from PySide6.QtCore import QRect
        text_rect = style.subElementRect(
            style.SubElement.SE_ItemViewItemText, opt, widget)

        fm = painter.fontMetrics()
        x = text_rect.left() + fm.horizontalAdvance(text[:pos])
        w = fm.horizontalAdvance(text[pos: pos + len(self._query)])
        highlight = QRect(x, text_rect.top() + 1, w, text_rect.height() - 2)

        painter.save()
        painter.fillRect(highlight, QColor(255, 220, 0, 140))
        painter.restore()


class FolderTreePanel(QWidget):
    """Resizable left-side panel showing a filesystem folder tree.

    Signals:
        navigate_to(path): emitted when the user activates a folder.
    """

    navigate_to = Signal(str)
    bookmarks_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._roots: list[str] = []
        self._watch_roots: set[str] = set()   # configured roots from set_roots()
        self._dynamic_root: str | None = None  # single temporary root for off-path nav

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Search bar — hidden until the user starts typing while tree is focused.
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Filter… (Esc to clear)")
        self._search_bar.hide()
        self._search_bar.textChanged.connect(self._on_search_changed)
        self._search_bar.installEventFilter(self)
        layout.addWidget(self._search_bar)

        self._tree = QTreeWidget()
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.setAnimated(False)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.installEventFilter(self)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._tree)

        self._delegate = _HighlightDelegate(self._tree)
        self._tree.setItemDelegate(self._delegate)

    # ------------------------------------------------------------------ public

    def set_roots(self, paths: list[str]) -> None:
        """Rebuild the tree from *paths* (watch directories)."""
        self._roots = list(paths)
        self._watch_roots = set(paths)
        self._dynamic_root = None
        self._tree.clear()
        for path in paths:
            self._add_node(None, path)

    def has_roots(self) -> bool:
        return self._tree.topLevelItemCount() > 0

    def add_root_if_missing(self, path: str) -> None:
        """Ensure *path* is reachable from the tree; add a root if needed."""
        for i in range(self._tree.topLevelItemCount()):
            root_path = self._tree.topLevelItem(i).data(0, Qt.UserRole)
            if path.startswith(root_path):
                return
        # Add the nearest existing ancestor as a root.
        candidate = path
        while candidate and not os.path.isdir(candidate):  # disk-io: walk up to find real ancestor
            candidate = os.path.dirname(candidate)
        if candidate and candidate not in self._roots:
            self._roots.append(candidate)
            self._dynamic_root = candidate
            self._add_node(None, candidate)

    def set_current_path(self, path: str) -> None:
        """Expand and select *path* in the tree (best-effort)."""
        self._clean_dynamic_root(path)
        self.add_root_if_missing(path)
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            root_path = item.data(0, Qt.UserRole)
            if root_path and (path.startswith(root_path + os.sep) or path == root_path):
                self._expand_to(item, path)
                return

    def _clean_dynamic_root(self, path: str) -> None:
        """Remove the dynamic root if *path* no longer lives under it."""
        if self._dynamic_root is None:
            return
        if path.startswith(self._dynamic_root + os.sep) or path == self._dynamic_root:
            return  # still needed
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == self._dynamic_root:
                self._tree.invisibleRootItem().removeChild(item)
                break
        self._roots.remove(self._dynamic_root)
        self._dynamic_root = None

    def focus_tree(self) -> None:
        self._tree.setFocus()

    def navigate_current_item(self) -> None:
        """Navigate to the currently selected tree item (for Alt+Down hotkey)."""
        item = self._tree.currentItem()
        if item:
            self._activate(item)

    def _show_context_menu(self, position):
        item = self._tree.itemAt(position)
        if not item:
            return

        path = item.data(0, Qt.UserRole)
        if not path or path == _PLACEHOLDER:
            return

        menu = QMenu()
        bookmarked_paths = {b.path for b in load_bookmarks()}

        if path in bookmarked_paths:
            action = menu.addAction("Remove from bookmarks")
            action.triggered.connect(lambda: self._remove_from_bookmarks(path))
        else:
            action = menu.addAction("Add to bookmarks")
            action.triggered.connect(lambda: self._add_to_bookmarks(path))

        menu.exec(self._tree.viewport().mapToGlobal(position))

    def _add_to_bookmarks(self, path: str):
        add_bookmark(path)
        self.bookmarks_changed.emit()

    def _remove_from_bookmarks(self, path: str):
        remove_bookmark(path)
        self.bookmarks_changed.emit()

    # ------------------------------------------------------------------ tree helpers

    def _add_node(self, parent: QTreeWidgetItem | None, path: str) -> QTreeWidgetItem:
        name = os.path.basename(path) or path
        if parent is None:
            item = QTreeWidgetItem(self._tree, [name])
        else:
            item = QTreeWidgetItem(parent, [name])
        item.setData(0, Qt.UserRole, path)
        # Always add a placeholder so the expand arrow appears.
        # If the dir has no subdirs, the placeholder is removed on expand.
        # This avoids a scandir call per node on NAS — only pay on user expand.
        QTreeWidgetItem(item, [_PLACEHOLDER])
        return item

    def _load_children(self, item: QTreeWidgetItem, path: str) -> None:
        try:
            entries = sorted(
                (e for e in os.scandir(path)  # disk-io: enumerate subdirectories on lazy expand
                 if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                key=lambda e: e.name.lower(),
            )
            for entry in entries:
                self._add_node(item, entry.path)
        except PermissionError:
            pass

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() == 1 and item.child(0).text(0) == _PLACEHOLDER:
            item.removeChild(item.child(0))
            path = item.data(0, Qt.UserRole)
            self._load_children(item, path)

    def _expand_to(self, item: QTreeWidgetItem, target: str) -> bool:
        """Recursively expand the tree down to *target*. Returns True on success."""
        item_path = item.data(0, Qt.UserRole)
        if item_path == target:
            self._tree.expandItem(item)  # always show current folder's children
            self._tree.setCurrentItem(item)
            self._tree.scrollToItem(item)
            return True
        if not target.startswith(item_path + os.sep):
            return False
        if not item.isExpanded():
            self._tree.expandItem(item)  # fires _on_item_expanded synchronously
        for i in range(item.childCount()):
            child = item.child(i)
            child_path = child.data(0, Qt.UserRole) or ""
            if target.startswith(child_path + os.sep) or target == child_path:
                return self._expand_to(child, target)
        return False

    # ------------------------------------------------------------------ activation

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        self._activate(item)

    def _activate(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.UserRole)
        if path and path != _PLACEHOLDER:
            self._clear_search()        # restore full tree before navigating
            self.navigate_to.emit(path) # triggers set_current_path via load_directory

    # ------------------------------------------------------------------ search

    def _on_search_changed(self, text: str) -> None:
        query = text.lower()
        self._delegate.set_query(query)
        if query:
            self._apply_filter(query)
            self._select_best_match(query)
        else:
            self._show_all_items()
        self._tree.update()

    def _apply_filter(self, query: str) -> None:
        for i in range(self._tree.topLevelItemCount()):
            self._filter_item(self._tree.topLevelItem(i), query)

    def _filter_item(self, item: QTreeWidgetItem, query: str) -> bool:
        """Hide non-matching items. Returns True if item or any descendant matches."""
        matches = _match_score(item.text(0), query) > 0
        child_match = any(
            self._filter_item(item.child(i), query)
            for i in range(item.childCount())
            if item.child(i).text(0) != _PLACEHOLDER
        )
        visible = matches or child_match
        item.setHidden(not visible)
        if child_match:
            item.setExpanded(True)
        return visible

    def _select_best_match(self, query: str) -> None:
        """Select the highest-scoring visible item for *query*."""
        best_item = None
        best_score = 0

        def _walk(item: QTreeWidgetItem) -> None:
            nonlocal best_item, best_score
            if item.isHidden():
                return
            score = _match_score(item.text(0), query)
            if score > best_score:
                best_score = score
                best_item = item
            for i in range(item.childCount()):
                _walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            _walk(self._tree.topLevelItem(i))

        if best_item is not None:
            self._tree.setCurrentItem(best_item)
            self._tree.scrollToItem(best_item)

    def _show_all_items(self) -> None:
        def _show(item: QTreeWidgetItem) -> None:
            item.setHidden(False)
            for i in range(item.childCount()):
                _show(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            _show(self._tree.topLevelItem(i))

    def _clear_search(self) -> None:
        self._search_bar.blockSignals(True)
        self._search_bar.clear()
        self._search_bar.blockSignals(False)
        self._search_bar.hide()
        self._delegate.set_query("")
        self._show_all_items()
        self._tree.update()

    # ------------------------------------------------------------------ event filter

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)

        key = event.key()
        mods = event.modifiers()

        if obj is self._tree:
            if key == Qt.Key_Escape:
                if self._search_bar.isVisible():
                    self._clear_search()
                    return True

            elif key in (Qt.Key_Return, Qt.Key_Enter):
                # Enter always opens (navigates to) the selected folder.
                item = self._tree.currentItem()
                if item:
                    self._activate(item)
                return True

            elif key == Qt.Key_Down and mods & Qt.AltModifier:
                # Alt+Down: navigate into the selected folder.
                item = self._tree.currentItem()
                if item:
                    self._activate(item)
                return True

            else:
                # Any printable character opens the search bar.
                # Content shortcuts are already disabled by HotkeyManager while
                # the tree panel has focus, so this won't swallow hotkeys.
                char = event.text()
                if char and char.isprintable():
                    self._search_bar.show()
                    self._search_bar.setFocus()
                    self._search_bar.setText(char)
                    return True

        elif obj is self._search_bar:
            if key == Qt.Key_Escape:
                self._clear_search()
                self._tree.setFocus()
                return True
            elif key in (Qt.Key_Up, Qt.Key_Down):
                # Move tree selection without closing search.
                self._tree.setFocus()
                new_event = QKeyEvent(event.type(), key, mods, event.text())
                QApplication.sendEvent(self._tree, new_event)
                self._search_bar.setFocus()
                return True
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                # Enter in search bar: navigate to the selected item.
                item = self._tree.currentItem()
                if item:
                    self._activate(item)
                return True

        return super().eventFilter(obj, event)
