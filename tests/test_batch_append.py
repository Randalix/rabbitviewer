# tests/test_batch_append.py
"""Tests for the scan-batch append fast path and initial thumbs loading.

Covers:
1. _add_image_batch append-only path (no filter active)
2. _add_image_batch fallback when filter is active
3. _on_initial_thumbs_received updating materialized placeholder labels
"""
import sys
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Ensure Qt stubs are available (test_filter_system pattern)
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    qtcore = sys.modules["PySide6.QtCore"]

    if not hasattr(qtcore, "QPoint"):
        class _QPoint:
            def __init__(self, x=0, y=0): self._x = x; self._y = y
            def x(self): return self._x
            def y(self): return self._y
            def __sub__(self, other):
                return _QPoint(self._x - other._x, self._y - other._y)
        qtcore.QPoint = _QPoint

    if not hasattr(qtcore, "Slot"):
        qtcore.Slot = lambda *a, **kw: (lambda fn: fn)

    if not hasattr(qtcore, "QSizeF"):
        class _QSizeF:
            def __init__(self, w=0, h=0): self._w = w; self._h = h
            def width(self): return self._w
            def height(self): return self._h
        qtcore.QSizeF = _QSizeF

    if not hasattr(qtcore, "QRectF"):
        class _QRectF:
            def __init__(self, *a): pass
            def width(self): return 0
            def height(self): return 0
        qtcore.QRectF = _QRectF

    if not hasattr(qtcore, "QRect"):
        class _QRect:
            def __init__(self, *a): pass
        qtcore.QRect = _QRect

    if not hasattr(qtcore, "QSize"):
        class _QSize:
            def __init__(self, w=0, h=0): self._w = w; self._h = h
            def width(self): return self._w
            def height(self): return self._h
        qtcore.QSize = _QSize

    if not hasattr(qtcore, "QEvent"):
        class _QEvent:
            Resize = 14
            def __init__(self, *a): pass
        qtcore.QEvent = _QEvent

    if not hasattr(qtcore, "QTimer"):
        class _QTimer:
            def __init__(self, *a): pass
            def setSingleShot(self, v): pass
            def setInterval(self, v): pass
            def start(self, *a): pass
            def stop(self): pass
            def isActive(self): return False
            @staticmethod
            def singleShot(ms, fn): pass
            @property
            def timeout(self): return MagicMock()
        qtcore.QTimer = _QTimer
    elif not hasattr(qtcore.QTimer, "singleShot"):
        qtcore.QTimer.singleShot = staticmethod(lambda ms, fn: None)

    if not hasattr(qtcore, "QElapsedTimer"):
        class _QElapsedTimer:
            def __init__(self): pass
            def start(self): pass
            def elapsed(self): return 0
        qtcore.QElapsedTimer = _QElapsedTimer

    qtgui = sys.modules.get("PySide6.QtGui")
    if qtgui and not hasattr(qtgui, "QImage"):
        class _QImage:
            Format_RGBA8888 = 0
            def __init__(self, *a): pass
            def isNull(self): return True
        qtgui.QImage = _QImage

    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    qt = qtcore.Qt
    for attr, val in [("Window", 0), ("LeftButton", 1), ("RightButton", 2),
                      ("Dialog", 0), ("ArrowCursor", 0),
                      ("WindowStaysOnTopHint", 0), ("WA_DeleteOnClose", 0),
                      ("WA_Hover", 0), ("Key_Return", 0), ("Key_Enter", 0),
                      ("QueuedConnection", 0), ("Horizontal", 1),
                      ("AlignCenter", 0x84), ("CaseInsensitive", 0)]:
        if not hasattr(qt, attr):
            setattr(qt, attr, val)

    if not hasattr(qt, "CursorShape"):
        class _CursorShape:
            ClosedHandCursor = 1
        qt.CursorShape = _CursorShape


_ensure_qt_stubs()

from gui.thumbnail_view import ThumbnailViewWidget, ImageState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_view(all_files=None, is_loading=False):
    """Build a minimal ThumbnailViewWidget stand-in with batch-append state."""
    view = object.__new__(ThumbnailViewWidget)

    view.all_files = list(all_files or [])
    view._all_files_set = set(view.all_files)
    view.current_files = list(view.all_files)
    view._is_loading = is_loading
    view._folder_is_cached = False

    view._current_filter = ""
    view._current_star_filter = [True] * 6
    view._current_tag_filter = []
    view._hidden_indices = set()
    view._visible_to_original_mapping = {}
    view._original_to_visible_mapping = {}
    view._visible_original_indices = []
    view._last_layout_file_count = len(view.all_files)

    # Build identity mapping for existing files
    view._path_to_idx = {}
    view.image_states = {}
    for i, f in enumerate(view.all_files):
        view._path_to_idx[f] = i
        view.image_states[i] = ImageState()
        view._visible_to_original_mapping[i] = i
        view._original_to_visible_mapping[i] = i
        view._visible_original_indices.append(i)

    view._filter_in_flight = False
    view._filter_pending = False
    view._filter_update_timer = MagicMock()
    view._virtual_grid = MagicMock()
    view._initial_thumb_paths = {}
    view._thumb_path_cache = {}
    view._pixmap_cache = {}
    view.labels = {}
    view.socket_client = MagicMock()
    view.filtersApplied = MagicMock()
    view._filtered_paths_ready = MagicMock()
    view._viewport_executor = MagicMock()
    view._startup_t0 = None
    view._startup_first_scan_progress = False
    view._startup_inline_thumb_count = 0
    view._needs_heatmap_seed = False
    view._hovered_label = None
    view.thumbnailLeft = MagicMock()
    view._benchmark_timer = MagicMock()

    return view


# ===================================================================
# Append fast path: no filter active
# ===================================================================

class TestAppendFastPath:

    def test_new_files_extend_mappings(self):
        """During scan, appending files extends visible mappings without clearing labels."""
        view = _make_view(all_files=["/img/a.jpg", "/img/b.jpg"], is_loading=True)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/c.jpg", "/img/d.jpg"])

        assert len(view.all_files) == 4
        assert len(view.current_files) == 4
        # New files get correct visible→original mapping
        assert view._visible_to_original_mapping[2] == 2
        assert view._visible_to_original_mapping[3] == 3
        assert view._original_to_visible_mapping[2] == 2
        assert view._original_to_visible_mapping[3] == 3

    def test_fast_path_calls_sync_viewport(self):
        """Scan fast path updates the virtual grid and syncs viewport."""
        view = _make_view(all_files=["/img/a.jpg"], is_loading=True)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        view._virtual_grid.set_total_items.assert_called_with(2)
        view._virtual_grid.update_layout.assert_called()

    def test_fast_path_does_not_clear_labels(self):
        """Scan fast path preserves existing materialized labels (hover state)."""
        existing_label = MagicMock()
        view = _make_view(all_files=["/img/a.jpg"], is_loading=True)
        view.labels[0] = existing_label

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        # Existing label should still be in labels dict
        assert view.labels[0] is existing_label
        # Virtual grid clear should NOT have been called
        view._virtual_grid.clear.assert_not_called()

    def test_fast_path_no_debounce_timer(self):
        """Scan fast path does not start the debounce timer."""
        view = _make_view(all_files=["/img/a.jpg"], is_loading=True)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        view._filter_update_timer.start.assert_not_called()

    def test_duplicate_files_ignored(self):
        """Files already in all_files are skipped."""
        view = _make_view(all_files=["/img/a.jpg", "/img/b.jpg"])

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/a.jpg", "/img/b.jpg"])

        assert len(view.all_files) == 2

    def test_post_scan_arrival_sorts_into_position(self):
        """Post-scan arrivals (is_loading=False) are sorted into position."""
        view = _make_view(all_files=["/img/a.jpg", "/img/c.jpg"], is_loading=False)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        # b.jpg should be sorted between a.jpg and c.jpg
        assert view.current_files == ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]
        assert len(view.all_files) == 3

    def test_uncached_folder_uses_fast_path(self):
        """Uncached folder (is_loading=True) takes fast path when no filter."""
        view = _make_view(is_loading=True)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/a.jpg", "/img/b.jpg"])

        view._virtual_grid.set_total_items.assert_called_with(2)
        view._filter_update_timer.start.assert_not_called()


# ===================================================================
# Filter active: full rebuild fallback
# ===================================================================

class TestAppendWithFilter:

    def test_filter_active_uses_debounce(self):
        """When filter hides files and is_loading=False, use debounce timer."""
        view = _make_view(all_files=["/img/a.jpg"], is_loading=False)
        view._hidden_indices = {0}  # filter is active

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        view._filter_update_timer.start.assert_called()

    def test_filter_active_during_scan_uses_immediate_apply(self):
        """When filter hides files and is_loading=True, apply immediately."""
        view = _make_view(all_files=["/img/a.jpg"], is_loading=True)
        view._hidden_indices = {0}  # filter is active

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/b.jpg"])

        # Should NOT use debounce timer
        view._filter_update_timer.start.assert_not_called()
        # _apply_filter_results rebuilds current_files from scratch
        # The hidden_indices will be recalculated


# ===================================================================
# _on_initial_thumbs_received updates materialized placeholders
# ===================================================================

class TestInitialThumbsUpdate:

    def test_updates_materialized_placeholder(self):
        """Thumb paths arriving after materialization update existing labels."""
        view = _make_view(all_files=["/img/a.jpg", "/img/b.jpg"])

        # Simulate materialized placeholder labels (no pixmap yet)
        label_a = MagicMock()
        label_b = MagicMock()
        view.labels = {0: label_a, 1: label_b}
        view.image_states = {0: ImageState(), 1: ImageState()}

        # Mock QImage to return a non-null image
        mock_image = MagicMock()
        mock_image.isNull.return_value = False
        mock_pixmap = MagicMock()

        with patch("gui.thumbnail_view.QImage", return_value=mock_image), \
             patch("gui.thumbnail_view.QPixmap") as MockPixmap:
            MockPixmap.fromImage.return_value = mock_pixmap
            ThumbnailViewWidget._on_initial_thumbs_received(view, {
                "/img/a.jpg": "/cache/a_thumb.jpg",
                "/img/b.jpg": "/cache/b_thumb.jpg",
            })

        # Both labels should have been updated
        label_a.updateThumbnail.assert_called_once_with(mock_pixmap)
        label_b.updateThumbnail.assert_called_once_with(mock_pixmap)
        assert view.image_states[0].loaded is True
        assert view.image_states[1].loaded is True
        # Thumb paths stored in cache
        assert view._thumb_path_cache[0] == "/cache/a_thumb.jpg"
        assert view._thumb_path_cache[1] == "/cache/b_thumb.jpg"

    def test_skips_already_loaded_pixmaps(self):
        """Labels that already have a pixmap are not updated again."""
        view = _make_view(all_files=["/img/a.jpg"])
        label_a = MagicMock()
        view.labels = {0: label_a}
        view._pixmap_cache[0] = MagicMock()  # already loaded

        with patch("gui.thumbnail_view.QImage") as MockQImage:
            ThumbnailViewWidget._on_initial_thumbs_received(view, {
                "/img/a.jpg": "/cache/a_thumb.jpg",
            })

        # Should not have created a QImage since pixmap already cached
        label_a.updateThumbnail.assert_not_called()

    def test_stores_paths_for_unknown_files(self):
        """Thumb paths for files not yet in all_files are stored for later."""
        view = _make_view(all_files=["/img/a.jpg"])

        ThumbnailViewWidget._on_initial_thumbs_received(view, {
            "/img/a.jpg": "/cache/a_thumb.jpg",
            "/img/unknown.jpg": "/cache/unknown_thumb.jpg",
        })

        assert view._initial_thumb_paths["/img/unknown.jpg"] == "/cache/unknown_thumb.jpg"

    def test_unmaterialized_labels_not_updated(self):
        """Files in all_files but not materialized just get thumb path cached."""
        view = _make_view(all_files=["/img/a.jpg", "/img/b.jpg"])
        # Only label 0 is materialized
        view.labels = {0: MagicMock()}

        mock_image = MagicMock()
        mock_image.isNull.return_value = False
        mock_pixmap = MagicMock()

        with patch("gui.thumbnail_view.QImage", return_value=mock_image), \
             patch("gui.thumbnail_view.QPixmap") as MockPixmap:
            MockPixmap.fromImage.return_value = mock_pixmap
            ThumbnailViewWidget._on_initial_thumbs_received(view, {
                "/img/a.jpg": "/cache/a_thumb.jpg",
                "/img/b.jpg": "/cache/b_thumb.jpg",
            })

        # Label 0 is materialized → updated
        view.labels[0].updateThumbnail.assert_called_once()
        # Label 1 not materialized → thumb path cached for lazy load
        assert view._thumb_path_cache[1] == "/cache/b_thumb.jpg"
        assert 1 not in view._pixmap_cache


# ===================================================================
# Thumb paths picked up from _initial_thumb_paths during batch append
# ===================================================================

class TestThumbPathPickup:

    def test_batch_picks_up_stored_thumb_paths(self):
        """_add_image_batch moves thumb paths from _initial_thumb_paths to _thumb_path_cache."""
        view = _make_view()
        # Simulate thumbs arriving before files
        view._initial_thumb_paths = {
            "/img/a.jpg": "/cache/a_thumb.jpg",
            "/img/b.jpg": "/cache/b_thumb.jpg",
        }

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._add_image_batch(view, ["/img/a.jpg", "/img/b.jpg"])

        assert view._thumb_path_cache[0] == "/cache/a_thumb.jpg"
        assert view._thumb_path_cache[1] == "/cache/b_thumb.jpg"
        # Should be removed from initial store
        assert "/img/a.jpg" not in view._initial_thumb_paths
        assert "/img/b.jpg" not in view._initial_thumb_paths
