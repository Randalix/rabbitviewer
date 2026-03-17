# gui/tiling_manager.py

from __future__ import annotations

from typing import List

from PySide6.QtWidgets import QWidget, QSplitter, QGridLayout, QHBoxLayout
from PySide6.QtCore import Qt, QSettings


class TilingManager(QWidget):
    """Manages a main content area with a draggable right dock for tiled widgets.

    Usage:
        tm = TilingManager()
        tm.set_main_content(stacked_widget)
        tm.dock_right(inspector)   # shows dock, arranges grid
        tm.undock(inspector)       # removes; hides dock when empty
    """

    _SETTINGS_GROUP = "TilingManager"
    _DEFAULT_RATIO = 0.75

    def __init__(self, parent=None):
        super().__init__(parent)
        self._docked: List[QWidget] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        layout.addWidget(self._splitter)

        # Left: placeholder until set_main_content is called.
        self._main_content: QWidget | None = None

        # Right: dock container with a grid inside.
        self._dock = QWidget()
        self._grid = QGridLayout(self._dock)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        self._splitter.addWidget(self._dock)
        self._dock.hide()

        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)

        self._restore_splitter_state()

    # ------------------------------------------------------------------ public

    def set_main_content(self, widget: QWidget) -> None:
        if self._main_content:
            self._splitter.replaceWidget(0, widget)
        else:
            self._splitter.insertWidget(0, widget)
        self._main_content = widget
        self._splitter.setStretchFactor(0, 3)

    def dock_right(self, widget: QWidget) -> None:
        self._docked.append(widget)
        if self._dock.isHidden():
            self._dock.show()
            total = self._splitter.width()
            if total > 0:
                left = int(total * self._DEFAULT_RATIO)
                self._splitter.setSizes([left, total - left])
        self._relayout()

    def undock(self, widget: QWidget) -> None:
        try:
            self._docked.remove(widget)
        except ValueError:
            return
        self._relayout()
        if not self._docked:
            self._dock.hide()

    @property
    def docked_count(self) -> int:
        return len(self._docked)

    def save_state(self) -> None:
        settings = QSettings("RabbitViewer", self._SETTINGS_GROUP)
        settings.setValue("splitter_state", self._splitter.saveState())
        settings.sync()

    # ------------------------------------------------------------------ layout

    def _relayout(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

        for r in range(self._grid.rowCount()):
            self._grid.setRowStretch(r, 0)
        for c in range(2):
            self._grid.setColumnStretch(c, 1)

        n = len(self._docked)
        if n == 0:
            return

        positions = self._compute_positions(n)
        for widget, (row, col, rowspan, colspan) in zip(self._docked, positions):
            self._grid.addWidget(widget, row, col, rowspan, colspan)
            widget.show()

        num_rows = positions[-1][0] + 1
        for r in range(num_rows):
            self._grid.setRowStretch(r, 1)

    @staticmethod
    def _compute_positions(n: int) -> list:
        """Return (row, col, rowspan, colspan) for each of *n* widgets.

        2-column grid. Odd last item spans full width.
        """
        if n == 1:
            return [(0, 0, 1, 2)]
        if n == 2:
            return [(0, 0, 1, 2), (1, 0, 1, 2)]

        positions = []
        has_remainder = n % 2 == 1
        for i in range(n):
            row = i // 2
            col = i % 2
            if has_remainder and i == n - 1:
                positions.append((row, 0, 1, 2))
            else:
                positions.append((row, col, 1, 1))
        return positions

    # ------------------------------------------------------------------ persistence

    def _restore_splitter_state(self) -> None:
        settings = QSettings("RabbitViewer", self._SETTINGS_GROUP)
        state = settings.value("splitter_state")
        if state:
            self._splitter.restoreState(state)
