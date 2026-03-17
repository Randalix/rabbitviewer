from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QMouseEvent
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from PySide6.QtWidgets import QScrollArea
    from gui.components.virtual_grid_manager import VirtualGridManager
    from gui.thumbnail_model import ThumbnailModel


@dataclass
class _IndicatorEntry:
    color: QColor
    ratio: float = 0.0
    click_callback: Optional[Callable] = None


class EdgeBar(QWidget):
    """Thin overlay bar at the top or bottom edge of a parent widget.

    Supports multiple named indicators, each with its own color and fill
    ratio.  Indicators are painted as centered rectangles, layered in
    registration order.
    """

    def __init__(self, parent: QWidget, edge: str, height: int = 3) -> None:
        super().__init__(parent)
        self._edge = edge
        self._bar_height = height
        self._indicators: Dict[str, _IndicatorEntry] = {}
        self._order: List[str] = []
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.hide()

    # -- public API -----------------------------------------------------------

    def add_indicator(
        self,
        name: str,
        color: QColor,
        click_callback: Optional[Callable] = None,
    ) -> None:
        if name not in self._indicators:
            self._indicators[name] = _IndicatorEntry(
                color=QColor(color), click_callback=click_callback,
            )
            self._order.append(name)

    def set_ratio(self, name: str, ratio: float) -> None:
        entry = self._indicators.get(name)
        if entry is None:
            return
        clamped = max(0.0, min(1.0, ratio))
        if entry.ratio == clamped:
            return
        entry.ratio = clamped
        self._sync_visibility()
        self.reposition()
        self.update()

    def remove_indicator(self, name: str) -> None:
        if name in self._indicators:
            del self._indicators[name]
            self._order.remove(name)
            self._sync_visibility()
            self.update()

    def reposition(self) -> None:
        p = self.parentWidget()
        if p is None:
            return
        w = p.width()
        if self._edge == "top":
            self.setGeometry(0, 0, w, self._bar_height)
        else:
            self.setGeometry(0, p.height() - self._bar_height, w, self._bar_height)
        self.raise_()

    # -- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        bar_w = self.width()
        if bar_w <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        for name in self._order:
            entry = self._indicators[name]
            if entry.ratio <= 0:
                continue
            fill_w = int(entry.ratio * bar_w)
            if fill_w <= 0:
                continue
            x = (bar_w - fill_w) // 2
            painter.fillRect(x, 0, fill_w, self.height(), entry.color)
        painter.end()

    # -- click dispatch -------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        for name in reversed(self._order):
            entry = self._indicators[name]
            if entry.ratio > 0 and entry.click_callback is not None:
                entry.click_callback()
                return

    # -- internal -------------------------------------------------------------

    def _sync_visibility(self) -> None:
        any_visible = any(e.ratio > 0 for e in self._indicators.values())
        if any_visible:
            self.show()
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.hide()
            self.setCursor(Qt.ArrowCursor)


class SelectionEdgeIndicator:
    """Drives two EdgeBars to show where selected items are relative to the viewport."""

    INDICATOR_NAME = "selection"

    def __init__(
        self,
        top_bar: EdgeBar,
        bottom_bar: EdgeBar,
        model: ThumbnailModel,
        grid: VirtualGridManager,
        scroll_area: QScrollArea,
        selection_source,
    ) -> None:
        self._top = top_bar
        self._bottom = bottom_bar
        self._model = model
        self._grid = grid
        self._scroll_area = scroll_area
        self._selection = selection_source

        color = top_bar._indicators.get(self.INDICATOR_NAME)
        if color is None:
            raise ValueError(
                "Register the 'selection' indicator on both bars before creating SelectionEdgeIndicator"
            )

    def update(self) -> None:
        sel = self._selection.current_selection
        if not sel:
            self._top.set_ratio(self.INDICATOR_NAME, 0)
            self._bottom.set_ratio(self.INDICATOR_NAME, 0)
            return

        model = self._model
        grid = self._grid
        thumb_size = grid._thumb_size

        topmost_y = None
        bottommost_y = None
        for path in sel:
            orig = model.path_to_idx.get(path)
            if orig is None:
                continue
            vis = model.original_to_visible.get(orig)
            if vis is None:
                continue
            y = grid._pos_y(vis)
            y_bottom = y + thumb_size
            if topmost_y is None or y < topmost_y:
                topmost_y = y
            if bottommost_y is None or y_bottom > bottommost_y:
                bottommost_y = y_bottom

        if topmost_y is None:
            self._top.set_ratio(self.INDICATOR_NAME, 0)
            self._bottom.set_ratio(self.INDICATOR_NAME, 0)
            return

        scroll_y = self._scroll_area.verticalScrollBar().value()
        viewport_h = self._scroll_area.viewport().height()
        center_y = scroll_y + viewport_h / 2
        half_vp = viewport_h / 2

        if half_vp <= 0:
            self._top.set_ratio(self.INDICATOR_NAME, 0)
            self._bottom.set_ratio(self.INDICATOR_NAME, 0)
            return

        top_ratio = max(0.0, min(1.0, (center_y - topmost_y) / half_vp))
        bot_ratio = max(0.0, min(1.0, (bottommost_y - center_y) / half_vp))

        self._top.set_ratio(self.INDICATOR_NAME, top_ratio)
        self._bottom.set_ratio(self.INDICATOR_NAME, bot_ratio)

    def scroll_to_nearest(self, direction: str) -> None:
        sel = self._selection.current_selection
        if not sel:
            return

        model = self._model
        grid = self._grid
        scroll_y = self._scroll_area.verticalScrollBar().value()
        viewport_h = self._scroll_area.viewport().height()
        viewport_top = scroll_y
        viewport_bottom = scroll_y + viewport_h

        candidates = []
        for path in sel:
            orig = model.path_to_idx.get(path)
            if orig is None:
                continue
            vis = model.original_to_visible.get(orig)
            if vis is None:
                continue
            item_y = grid._pos_y(vis)
            item_bottom = item_y + grid._thumb_size
            if direction == "up" and item_y < viewport_top:
                candidates.append(vis)
            elif direction == "down" and item_bottom > viewport_bottom:
                candidates.append(vis)

        if not candidates:
            return

        if direction == "up":
            target = max(candidates, key=lambda vi: grid._pos_y(vi))
        else:
            target = min(candidates, key=lambda vi: grid._pos_y(vi))

        grid.ensure_visible(target, center=True)
