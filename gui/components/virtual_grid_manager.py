from __future__ import annotations

from math import floor
from typing import Callable, Dict, Optional, Tuple, TYPE_CHECKING

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QScrollArea, QWidget

if TYPE_CHECKING:
    from gui.thumbnail_view import ThumbnailLabel


class VirtualGridManager(QObject):
    """Virtual scrolling grid that materializes only viewport-visible widgets.

    Replaces GridLayoutManager.  No QGridLayout — labels are positioned
    absolutely via ``label.move(x, y)`` on a plain QWidget container whose
    fixed height drives the scroll range.
    """

    layoutChanged = Signal()

    _BUFFER_ROWS = 3  # extra rows above/below the viewport

    def __init__(
        self,
        container: QWidget,
        scroll_area: QScrollArea,
        thumbnail_size: int,
        spacing: int,
    ) -> None:
        super().__init__()
        self._container = container
        self._scroll_area = scroll_area
        self._thumb_size = thumbnail_size
        self._spacing = spacing
        self._columns = 1
        self._total_items = 0

        # Materialized window: visible_idx in [_mat_start, _mat_end)
        self._mat_start = 0
        self._mat_end = 0
        self._mat_labels: Dict[int, ThumbnailLabel] = {}

    # ------------------------------------------------------------------
    # Layout geometry (pure math)
    # ------------------------------------------------------------------

    @property
    def _cell(self) -> int:
        return self._thumb_size + self._spacing

    @property
    def _total_rows(self) -> int:
        if self._total_items <= 0 or self._columns <= 0:
            return 0
        return (self._total_items + self._columns - 1) // self._columns

    @property
    def _x_offset(self) -> int:
        """Horizontal offset to center the grid within the viewport."""
        grid_width = self._spacing + self._columns * self._cell
        available = self._get_available_width()
        return max(0, (available - grid_width) // 2)

    def _pos_x(self, visible_idx: int) -> int:
        col = visible_idx % self._columns
        return self._x_offset + self._spacing + col * self._cell

    def _pos_y(self, visible_idx: int) -> int:
        row = visible_idx // self._columns
        return self._spacing + row * self._cell

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def columns(self) -> int:
        return self._columns

    def calculate_columns(self, available_width: int) -> int:
        content_width = available_width - 2 * self._spacing
        if content_width < self._thumb_size:
            return 1
        cell = self._cell
        if cell <= 0:
            return 1
        return max(1, floor((content_width + self._spacing) / cell))

    def set_total_items(self, count: int) -> None:
        self._total_items = count
        self._update_container_height()

    def update_layout(self) -> None:
        """Recalculate column count from current width; reposition labels if columns changed."""
        available_width = self._get_available_width()
        new_columns = self.calculate_columns(available_width)
        if new_columns != self._columns:
            self._columns = new_columns
            self._update_container_height()
            self._reposition_materialized()
            self.layoutChanged.emit()
        else:
            # Column count unchanged but viewport width may have shifted the
            # centering offset — reposition to keep the grid centered.
            self._reposition_materialized()

    def get_visible_rows(self) -> Tuple[int, int]:
        """First and last visible rows, based on scroll position."""
        cell = self._cell
        if cell <= 0:
            return 0, -1

        scroll_y = self._scroll_area.verticalScrollBar().value()
        viewport_height = self._scroll_area.viewport().height()

        visible_y_start = scroll_y
        visible_y_end = scroll_y + viewport_height

        first_row = max(0, floor((visible_y_start - self._spacing) / cell))
        last_row = max(0, floor((visible_y_end - self._spacing) / cell))
        return int(first_row), int(last_row)

    def get_widget_at_position(self, pos: QPoint) -> Optional[int]:
        """Pure-math hit test: return visible_idx at *pos* (container coords), or None."""
        x = pos.x() - self._x_offset - self._spacing
        y = pos.y() - self._spacing
        if x < 0 or y < 0:
            return None

        cell = self._cell
        col = int(x // cell)
        row = int(y // cell)

        # In the gap between items?
        if (x % cell) > self._thumb_size:
            return None
        if (y % cell) > self._thumb_size:
            return None
        if not (0 <= col < self._columns):
            return None

        vis_idx = row * self._columns + col
        if 0 <= vis_idx < self._total_items:
            return vis_idx
        return None

    def ensure_visible(self, visible_idx: int, center: bool = False) -> None:
        """Scroll so that *visible_idx* is in the viewport (pure math, no widget needed)."""
        if visible_idx < 0 or visible_idx >= self._total_items:
            return

        target_y = self._pos_y(visible_idx)

        vbar = self._scroll_area.verticalScrollBar()
        viewport_h = self._scroll_area.viewport().height()

        if center:
            vbar.setValue(max(0, target_y - (viewport_h - self._thumb_size) // 2))
        else:
            current = vbar.value()
            # Already visible?
            if current <= target_y and target_y + self._thumb_size <= current + viewport_h:
                return
            # Scroll minimally to make it visible.
            if target_y < current:
                vbar.setValue(target_y)
            else:
                vbar.setValue(target_y + self._thumb_size - viewport_h)

    def sync_viewport(
        self,
        get_label: Callable[[int], ThumbnailLabel],
        recycle_label: Callable[[ThumbnailLabel], None],
    ) -> None:
        """Materialize labels for the visible range; recycle labels that scrolled out."""
        first_row, last_row = self.get_visible_rows()
        buf_first = max(0, first_row - self._BUFFER_ROWS)
        buf_last = min(self._total_rows - 1, last_row + self._BUFFER_ROWS) if self._total_rows > 0 else -1

        if buf_last < buf_first:
            # Nothing visible — recycle everything.
            new_start, new_end = 0, 0
        else:
            new_start = buf_first * self._columns
            new_end = min(self._total_items, (buf_last + 1) * self._columns)

        # Recycle labels leaving the window.
        for vis_idx in list(self._mat_labels):
            if vis_idx < new_start or vis_idx >= new_end:
                label = self._mat_labels.pop(vis_idx)
                recycle_label(label)

        # Materialize labels entering the window.
        for vis_idx in range(new_start, new_end):
            if vis_idx not in self._mat_labels:
                label = get_label(vis_idx)
                label.move(self._pos_x(vis_idx), self._pos_y(vis_idx))
                label.show()
                self._mat_labels[vis_idx] = label

        self._mat_start = new_start
        self._mat_end = new_end

    def clear(self, recycle_label: Callable[[ThumbnailLabel], None]) -> None:
        """Recycle all materialized labels."""
        for label in self._mat_labels.values():
            recycle_label(label)
        self._mat_labels.clear()
        self._mat_start = 0
        self._mat_end = 0

    def get_position(self, visible_idx: int) -> Tuple[int, int]:
        return divmod(visible_idx, self._columns)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reposition_materialized(self) -> None:
        """Move all materialized labels to their current grid positions."""
        for vis_idx, label in self._mat_labels.items():
            label.move(self._pos_x(vis_idx), self._pos_y(vis_idx))

    def _get_available_width(self) -> int:
        w = self._scroll_area.viewport().width()
        if w <= 0:
            w = self._scroll_area.width() if self._scroll_area.width() > 0 else 800
        return w

    def _update_container_height(self) -> None:
        h = self._spacing + self._total_rows * self._cell if self._total_rows > 0 else 0
        self._container.setFixedHeight(max(0, h))
