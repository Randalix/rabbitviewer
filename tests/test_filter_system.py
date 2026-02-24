# tests/test_filter_system.py
"""
Tests for the rating/text filter system.

Covers the two bugs fixed:
1. _is_loading stuck True on cached folders, preventing filter queries
2. Missing ThumbnailViewWidget.clear_filter() method
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Extended Qt stubs — ThumbnailViewWidget needs more surface than conftest
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    qtcore = sys.modules["PySide6.QtCore"]

    if not hasattr(qtcore, "QPoint"):
        class _QPoint:
            def __init__(self, x=0, y=0): self._x = x; self._y = y
            def x(self): return self._x
            def y(self): return self._y
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

    # Timer stubs
    if not hasattr(qtcore, "QTimer"):
        class _QTimer:
            def __init__(self, *a): pass
            def setSingleShot(self, v): pass
            def setInterval(self, v): pass
            def start(self, *a): pass
            def stop(self): pass
            def isActive(self): return False
            @property
            def timeout(self): return MagicMock()
        qtcore.QTimer = _QTimer

    if not hasattr(qtcore, "QElapsedTimer"):
        class _QElapsedTimer:
            def __init__(self): pass
            def start(self): pass
            def elapsed(self): return 0
        qtcore.QElapsedTimer = _QElapsedTimer

    # QtGui
    qtgui = sys.modules.get("PySide6.QtGui")
    if qtgui is None:
        qtgui = types.ModuleType("PySide6.QtGui")
        sys.modules["PySide6.QtGui"] = qtgui
        sys.modules["PySide6"].QtGui = qtgui

    if not hasattr(qtgui, "QImage"):
        class _QImage:
            Format_RGBA8888 = 0
            def __init__(self, *a): pass
            def isNull(self): return True
        qtgui.QImage = _QImage

    for name in ("QPixmap", "QColor", "QMouseEvent", "QKeyEvent", "QCursor",
                 "QKeySequence", "QPainter"):
        if not hasattr(qtgui, name):
            setattr(qtgui, name, type(name, (), {"__init__": lambda self, *a, **kw: None}))

    if not hasattr(qtgui, "QShortcut"):
        class _QShortcut:
            def __init__(self, *a, **kw):
                self.activated = MagicMock()
        qtgui.QShortcut = _QShortcut

    # QtWidgets
    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    if qtwidgets is None:
        qtwidgets = types.ModuleType("PySide6.QtWidgets")
        sys.modules["PySide6.QtWidgets"] = qtwidgets
        sys.modules["PySide6"].QtWidgets = qtwidgets

    if not hasattr(qtwidgets, "QWidget"):
        class _QWidget:
            def __init__(self, *a, **kw): pass
            def size(self): return qtcore.QSizeF(800, 600)
            def width(self): return 800
            def height(self): return 600
            def update(self): pass
            def isVisible(self): return True
            def setAttribute(self, *a): pass
            def setMouseTracking(self, v): pass
            def installEventFilter(self, f): pass
        qtwidgets.QWidget = _QWidget

    if not hasattr(qtwidgets, "QFrame"):
        class _QFrame(qtwidgets.QWidget):
            pass
        qtwidgets.QFrame = _QFrame

    for name in ("QScrollArea", "QVBoxLayout", "QGridLayout", "QHBoxLayout",
                 "QMainWindow", "QApplication"):
        if not hasattr(qtwidgets, name):
            cls = type(name, (), {
                "__init__": lambda self, *a, **kw: None,
                "viewport": lambda self: MagicMock(),
                "verticalScrollBar": lambda self: MagicMock(),
                "addWidget": lambda self, *a: None,
                "addLayout": lambda self, *a: None,
                "setContentsMargins": lambda self, *a: None,
                "setSpacing": lambda self, *a: None,
                "setMouseTracking": lambda self, v: None,
            })
            setattr(qtwidgets, name, cls)

    if not hasattr(qtwidgets, "QLabel"):
        class _QLabel(qtwidgets.QWidget):
            def __init__(self, *a, **kw): pass
        qtwidgets.QLabel = _QLabel

    if not hasattr(qtwidgets, "QPushButton"):
        class _QPushButton(qtwidgets.QWidget):
            def __init__(self, *a, **kw): pass
            def setFixedSize(self, *a): pass
            def setStyleSheet(self, s): pass
            def setMouseTracking(self, v): pass
            def setAttribute(self, *a): pass
        qtwidgets.QPushButton = _QPushButton

    if not hasattr(qtwidgets, "QDialog"):
        class _QDialog(qtwidgets.QWidget):
            def __init__(self, *a, **kw): pass
            def setWindowTitle(self, t): pass
            def setModal(self, m): pass
            def setWindowFlags(self, f): pass
            def resize(self, *a): pass
            def show(self): pass
            def hide(self): pass
            def close(self): pass
            def accept(self): pass
        qtwidgets.QDialog = _QDialog

    if not hasattr(qtwidgets, "QLineEdit"):
        class _QLineEdit(qtwidgets.QWidget):
            def __init__(self, *a): pass
            def setPlaceholderText(self, t): pass
            @property
            def textEdited(self): return MagicMock()
            def setFocus(self): pass
            def selectAll(self): pass
            def text(self): return ""
            def clear(self): pass
        qtwidgets.QLineEdit = _QLineEdit

    # Qt namespace constants
    qt = qtcore.Qt
    for attr, val in [("Window", 0), ("LeftButton", 1), ("Dialog", 0),
                      ("WindowStaysOnTopHint", 0), ("WA_DeleteOnClose", 0),
                      ("WA_Hover", 0), ("Key_Return", 0), ("Key_Enter", 0),
                      ("QueuedConnection", 0), ("Horizontal", 1),
                      ("AlignCenter", 0x84)]:
        if not hasattr(qt, attr):
            setattr(qt, attr, val)


_ensure_qt_stubs()


# ---------------------------------------------------------------------------
# Lightweight stand-in for ThumbnailViewWidget filter state.
# ---------------------------------------------------------------------------

def _make_filter_view(all_files=None, is_loading=False, socket_client=None):
    """Build a minimal stand-in with filter state and methods from ThumbnailViewWidget."""
    from gui.thumbnail_view import ThumbnailViewWidget

    view = object.__new__(ThumbnailViewWidget)

    view.all_files = list(all_files or [])
    view._all_files_set = set(view.all_files)
    view.current_files = list(view.all_files)
    view._is_loading = is_loading
    view._folder_is_cached = False

    view._current_filter = ""
    view._current_star_filter = [True, True, True, True, True, True]
    view._hidden_indices = set()
    view._visible_to_original_mapping = {}
    view._original_to_visible_mapping = {}
    view._visible_original_indices = []
    view._last_layout_file_count = 0

    view._filter_in_flight = False
    view._filter_pending = False

    view._filter_update_timer = MagicMock()
    view._grid_layout_manager = None
    view.labels = {}
    view.socket_client = socket_client

    view.filtersApplied = MagicMock()
    view._filtered_paths_ready = MagicMock()
    view._viewport_executor = MagicMock()
    view._label_tick_timer = MagicMock()
    view._pending_labels = []
    view.image_states = {}

    return view


def _insert_with_rating(db, file_path, rating):
    """Insert a DB record with a specific rating via direct SQL (no filesystem)."""
    import hashlib, time as _time
    path_hash = hashlib.md5(file_path.encode()).hexdigest()
    now = _time.time()
    with db._lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO image_metadata "
            "(file_path, path_hash, rating, file_size, mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0, ?, ?)",
            (file_path, path_hash, rating, now, now),
        )
        db.conn.commit()


# ===================================================================
# Bug 1: _is_loading cleared for cached folders
# ===================================================================

class TestIsLoadingCachedFolder:

    def test_cached_folder_clears_is_loading(self):
        """_on_initial_files_received sets _is_loading=False when files come from DB cache."""
        from gui.thumbnail_view import ThumbnailViewWidget

        view = _make_filter_view(is_loading=True)
        view._pending_labels = []
        view._startup_t0 = None
        view._startup_first_scan_progress = False
        view._startup_inline_thumb_count = 0
        view._startup_first_inline_thumb = False
        view._initial_thumb_paths = {}
        view._path_to_idx = {}

        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]

        with patch("gui.thumbnail_view.event_system"), \
             patch.object(ThumbnailViewWidget, "_tick_label_creation"):
            ThumbnailViewWidget._on_initial_files_received(view, files)

        assert view._folder_is_cached is True
        assert view._is_loading is False

    def test_empty_folder_keeps_is_loading(self):
        """_on_initial_files_received with no files (new folder) keeps _is_loading=True."""
        from gui.thumbnail_view import ThumbnailViewWidget

        view = _make_filter_view(is_loading=True)
        view._pending_labels = []
        view._startup_t0 = None
        view._startup_first_scan_progress = False
        view._startup_inline_thumb_count = 0
        view._startup_first_inline_thumb = False
        view._initial_thumb_paths = {}
        view._path_to_idx = {}

        with patch("gui.thumbnail_view.event_system"), \
             patch.object(ThumbnailViewWidget, "_tick_label_creation"):
            ThumbnailViewWidget._on_initial_files_received(view, [])

        assert view._folder_is_cached is False
        assert view._is_loading is True

    def test_cached_folder_reapply_uses_daemon_path(self):
        """After cached folder clears _is_loading, reapply_filters submits to the daemon."""
        from gui.thumbnail_view import ThumbnailViewWidget

        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, is_loading=False, socket_client=mock_client)

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget.reapply_filters(view)

        assert view._filter_in_flight is True
        view._viewport_executor.submit.assert_called_once()

    def test_is_loading_true_takes_fast_path(self):
        """When _is_loading is True, reapply_filters shows all files (no daemon query)."""
        from gui.thumbnail_view import ThumbnailViewWidget

        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, is_loading=True, socket_client=mock_client)
        view._current_star_filter = [False, False, True, False, False, False]

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget.reapply_filters(view)

        view._viewport_executor.submit.assert_not_called()
        assert view._filter_in_flight is False


# ===================================================================
# Bug 2: clear_filter() method
# ===================================================================

class TestClearFilter:

    def test_clear_filter_resets_text(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        view._current_filter = "sunset"
        ThumbnailViewWidget.clear_filter(view)
        assert view._current_filter == ""

    def test_clear_filter_resets_star_states(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        view._current_star_filter = [False, False, True, False, False, False]
        ThumbnailViewWidget.clear_filter(view)
        assert view._current_star_filter == [True, True, True, True, True, True]

    def test_clear_filter_resets_hidden_indices(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        view._hidden_indices = {0, 3, 7}
        ThumbnailViewWidget.clear_filter(view)
        assert view._hidden_indices == set()

    def test_clear_filter_starts_debounce_timer(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        view._current_filter = "test"
        view._current_star_filter = [False] * 6
        ThumbnailViewWidget.clear_filter(view)
        view._filter_update_timer.start.assert_called_once()


# ===================================================================
# Filter in-flight / pending queuing
# ===================================================================

class TestFilterQueuing:

    def test_second_call_queues_pending(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        mock_client = MagicMock()
        files = ["/img/a.jpg"]
        view = _make_filter_view(all_files=files, socket_client=mock_client)
        view._filter_in_flight = True

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget.reapply_filters(view)

        assert view._filter_pending is True
        view._viewport_executor.submit.assert_not_called()

    def test_pending_resubmits_after_completion(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, socket_client=mock_client)
        view._filter_in_flight = True
        view._filter_pending = True

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._on_filtered_paths_ready(view, set(files))

        assert view._filter_in_flight is True
        assert view._filter_pending is False
        view._viewport_executor.submit.assert_called_once()


# ===================================================================
# apply_filter / apply_star_filter
# ===================================================================

class TestApplyFilter:

    def test_apply_filter_sets_text(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        ThumbnailViewWidget.apply_filter(view, "beach")
        assert view._current_filter == "beach"
        view._filter_update_timer.start.assert_called()

    def test_apply_star_filter_sets_states(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        view = _make_filter_view()
        new_states = [True, False, True, False, True, False]
        ThumbnailViewWidget.apply_star_filter(view, new_states)
        assert view._current_star_filter == new_states
        view._filter_update_timer.start.assert_called()


# ===================================================================
# _apply_filter_results hides correct indices
# ===================================================================

class TestApplyFilterResults:

    def test_hides_non_visible_paths(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg", "/img/d.jpg"]
        view = _make_filter_view(all_files=files)
        visible = {"/img/a.jpg", "/img/c.jpg"}
        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._apply_filter_results(view, visible)
        assert view._hidden_indices == {1, 3}

    def test_all_visible_clears_hidden(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files)
        view._hidden_indices = {0, 1}
        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._apply_filter_results(view, set(files))
        assert view._hidden_indices == set()

    def test_none_visible_hides_all(self):
        from gui.thumbnail_view import ThumbnailViewWidget
        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]
        view = _make_filter_view(all_files=files)
        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._apply_filter_results(view, set())
        assert view._hidden_indices == {0, 1, 2}


# ===================================================================
# Database filtering (get_filtered_file_paths)
# ===================================================================

class TestDatabaseFiltering:

    def test_all_stars_enabled_returns_everything(self, tmp_env):
        db = tmp_env["db"]
        for path, rating in [("/img/a.jpg", 0), ("/img/b.jpg", 3), ("/img/c.jpg", 5)]:
            _insert_with_rating(db, path, rating)

        result = db.get_filtered_file_paths("", [True] * 6)
        assert set(result) == {"/img/a.jpg", "/img/b.jpg", "/img/c.jpg"}

    def test_filter_by_rating(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/unrated.jpg", 0)
        _insert_with_rating(db, "/img/good.jpg", 4)
        _insert_with_rating(db, "/img/best.jpg", 5)

        stars = [False, False, False, False, True, True]
        result = db.get_filtered_file_paths("", stars)
        assert set(result) == {"/img/good.jpg", "/img/best.jpg"}

    def test_no_stars_returns_empty(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/a.jpg", 3)

        result = db.get_filtered_file_paths("", [False] * 6)
        assert result == []

    def test_text_filter(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/photos/beach.jpg", 0)
        _insert_with_rating(db, "/photos/mountain.jpg", 0)

        result = db.get_filtered_file_paths("beach", [True] * 6)
        assert result == ["/photos/beach.jpg"]

    def test_combined_text_and_star_filter(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/beach_good.jpg", 4)
        _insert_with_rating(db, "/img/beach_ok.jpg", 2)
        _insert_with_rating(db, "/img/mountain.jpg", 4)

        stars = [False, False, False, False, True, False]
        result = db.get_filtered_file_paths("beach", stars)
        assert result == ["/img/beach_good.jpg"]


# ===================================================================
# FilterDialog.clear_filter syncs button state
# ===================================================================

class TestFilterDialogClear:

    def test_clear_filter_calls_set_state_on_all_buttons(self):
        """clear_filter() calls set_state(True) on every star button."""
        from gui.filter_dialog import FilterDialog

        with patch("gui.filter_dialog.StarButton") as MockBtn, \
             patch("gui.filter_dialog.StarDragContext"):
            def make_btn(**kw):
                btn = MagicMock()
                # Wire up set_state to call the handler like the real button does
                def side_effect(state, b=btn, idx=kw.get("index", 0)):
                    b._current_state = state
                    dialog._on_star_button_toggled(idx, state)
                btn.set_state.side_effect = side_effect
                btn._current_state = kw.get("initial_state", True)
                return btn
            MockBtn.side_effect = make_btn

            dialog = FilterDialog()

        dialog.star_states = [False, False, True, False, False, False]
        dialog.clear_filter()

        assert dialog.star_states == [True, True, True, True, True, True]
        for btn in dialog.star_buttons:
            btn.set_state.assert_called_with(True)
