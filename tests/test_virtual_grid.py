# tests/test_virtual_grid.py
"""Unit tests for VirtualGridManager position math and sync_viewport logic."""
import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal Qt stubs so VirtualGridManager can be imported without PySide6.
# ---------------------------------------------------------------------------

def _ensure_stubs():
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return  # conftest already provides stubs

    if not hasattr(qtcore, "QPoint"):
        class _QPoint:
            def __init__(self, x=0, y=0): self._x = x; self._y = y
            def x(self): return self._x
            def y(self): return self._y
        qtcore.QPoint = _QPoint

    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    if qtwidgets and not hasattr(qtwidgets, "QScrollArea"):
        for name in ("QScrollArea", "QWidget"):
            if not hasattr(qtwidgets, name):
                setattr(qtwidgets, name, type(name, (), {
                    "__init__": lambda self, *a, **kw: None,
                    "viewport": lambda self: MagicMock(),
                    "verticalScrollBar": lambda self: MagicMock(),
                    "width": lambda self: 800,
                }))


_ensure_stubs()

from gui.components.virtual_grid_manager import VirtualGridManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(thumb_size=128, spacing=5, viewport_width=800, viewport_height=600, scroll_y=0):
    """Create a VirtualGridManager with mock container and scroll area."""
    container = MagicMock()
    container.y.return_value = 0
    container.setFixedHeight = MagicMock()

    viewport = MagicMock()
    viewport.width.return_value = viewport_width
    viewport.height.return_value = viewport_height

    vbar = MagicMock()
    vbar.value.return_value = scroll_y

    scroll_area = MagicMock()
    scroll_area.viewport.return_value = viewport
    scroll_area.verticalScrollBar.return_value = vbar
    scroll_area.width.return_value = viewport_width

    mgr = object.__new__(VirtualGridManager)
    # Bypass QObject.__init__ which would fail without a real Qt app
    mgr._container = container
    mgr._scroll_area = scroll_area
    mgr._thumb_size = thumb_size
    mgr._spacing = spacing
    mgr._columns = 1
    mgr._total_items = 0
    mgr._mat_start = 0
    mgr._mat_end = 0
    mgr._mat_labels = {}
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestColumnCalculation:

    def test_single_column_narrow(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_width=100)
        assert mgr.calculate_columns(100) == 1

    def test_exact_fit(self):
        # 2 thumbnails: 5 + 128 + 5 + 128 + 5 = 271
        mgr = _make_manager(thumb_size=128, spacing=5)
        assert mgr.calculate_columns(271) == 2

    def test_multiple_columns(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        # content_width = 800 - 10 = 790. (790 + 5) / 133 = 5.97 → 5 columns
        cols = mgr.calculate_columns(800)
        assert cols == 5

    def test_zero_width(self):
        mgr = _make_manager()
        assert mgr.calculate_columns(0) == 1


class TestPositionMath:

    def test_first_item(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        assert mgr._pos_x(0) == 5
        assert mgr._pos_y(0) == 5

    def test_second_row(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        # Item at index 6 → row 1, col 0
        assert mgr._pos_x(6) == 5
        assert mgr._pos_y(6) == 5 + 133  # spacing + cell

    def test_third_column(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        # Item at index 2 → row 0, col 2
        assert mgr._pos_x(2) == 5 + 2 * 133


class TestTotalRows:

    def test_empty(self):
        mgr = _make_manager()
        mgr._total_items = 0
        mgr._columns = 6
        assert mgr._total_rows == 0

    def test_exact_rows(self):
        mgr = _make_manager()
        mgr._total_items = 12
        mgr._columns = 6
        assert mgr._total_rows == 2

    def test_partial_row(self):
        mgr = _make_manager()
        mgr._total_items = 7
        mgr._columns = 6
        assert mgr._total_rows == 2


class TestContainerHeight:

    def test_height_set(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        mgr.set_total_items(12)
        # 2 rows: spacing + 2 * cell = 5 + 2 * 133 = 271
        mgr._container.setFixedHeight.assert_called_with(271)

    def test_height_zero_items(self):
        mgr = _make_manager()
        mgr._columns = 6
        mgr.set_total_items(0)
        mgr._container.setFixedHeight.assert_called_with(0)


class TestGetVisibleRows:

    def test_top_of_scroll(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_height=600, scroll_y=0)
        mgr._columns = 6
        mgr._total_items = 100
        first, last = mgr.get_visible_rows()
        assert first == 0
        assert last >= 3  # 600 / 133 ≈ 4.5

    def test_scrolled_down(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_height=600, scroll_y=400)
        mgr._columns = 6
        mgr._total_items = 100
        first, last = mgr.get_visible_rows()
        assert first >= 2
        assert last >= 5


class TestHitTest:

    def test_hit_first_item(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        mgr._total_items = 100
        # Center of first item
        from PySide6.QtCore import QPoint
        pos = QPoint(5 + 64, 5 + 64)
        assert mgr.get_widget_at_position(pos) == 0

    def test_hit_gap(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        mgr._total_items = 100
        from PySide6.QtCore import QPoint
        # In the gap between first and second item
        pos = QPoint(5 + 128 + 2, 5 + 64)  # x in gap
        assert mgr.get_widget_at_position(pos) is None

    def test_hit_second_item(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        mgr._total_items = 100
        from PySide6.QtCore import QPoint
        pos = QPoint(5 + 133 + 64, 5 + 64)  # second column center
        assert mgr.get_widget_at_position(pos) == 1

    def test_hit_out_of_range(self):
        mgr = _make_manager(thumb_size=128, spacing=5)
        mgr._columns = 6
        mgr._total_items = 3
        from PySide6.QtCore import QPoint
        pos = QPoint(5 + 4 * 133 + 64, 5 + 64)  # column 4, but only 3 items
        assert mgr.get_widget_at_position(pos) is None


class TestSyncViewport:

    def test_materialize_visible(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_height=300, scroll_y=0)
        mgr._columns = 6
        mgr._total_items = 100

        created = {}
        def get_label(vis_idx):
            label = MagicMock()
            created[vis_idx] = label
            return label

        recycled = []
        def recycle_label(label):
            recycled.append(label)

        mgr.sync_viewport(get_label, recycle_label)

        # Should have materialized some labels but not all 100
        assert len(created) > 0
        assert len(created) < 100
        assert mgr._mat_start == 0
        assert mgr._mat_end == len(created)

    def test_scroll_recycles_and_creates(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_height=300, scroll_y=0)
        mgr._columns = 6
        mgr._total_items = 200

        labels = {}
        counter = [0]
        def get_label(vis_idx):
            label = MagicMock(name=f"label_{vis_idx}")
            labels[vis_idx] = label
            counter[0] += 1
            return label

        recycled = []
        def recycle_label(label):
            recycled.append(label)

        # First sync at top
        mgr.sync_viewport(get_label, recycle_label)
        initial_count = counter[0]
        assert initial_count > 0

        # Scroll down significantly
        mgr._scroll_area.verticalScrollBar().value.return_value = 1000
        mgr.sync_viewport(get_label, recycle_label)

        # Should have recycled some labels from the top
        assert len(recycled) > 0
        # Should have created new labels at the bottom
        assert counter[0] > initial_count

    def test_clear_recycles_all(self):
        mgr = _make_manager(thumb_size=128, spacing=5, viewport_height=300, scroll_y=0)
        mgr._columns = 6
        mgr._total_items = 50

        def get_label(vis_idx):
            return MagicMock()

        recycled = []
        def recycle_label(label):
            recycled.append(label)

        mgr.sync_viewport(get_label, recycle_label)
        mat_count = len(mgr._mat_labels)
        assert mat_count > 0

        mgr.clear(recycle_label)
        assert len(recycled) == mat_count
        assert len(mgr._mat_labels) == 0
