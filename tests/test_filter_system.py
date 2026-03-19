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
            def setWindowTitle(self, t): pass
            def setMinimumSize(self, w, h): pass
            def resize(self, w, h): pass
            def restoreGeometry(self, g): pass
            def saveGeometry(self): return b""
            def rect(self): return qtcore.QRectF()
            def setCursor(self, c): pass
            def showEvent(self, e): pass
            def resizeEvent(self, e): pass
            def mousePressEvent(self, e): pass
            def mouseReleaseEvent(self, e): pass
            def mouseMoveEvent(self, e): pass
            def mouseDoubleClickEvent(self, e): pass
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
            def __init__(self, *a, **kw): pass
            def setPlaceholderText(self, t): pass
            @property
            def textEdited(self): return MagicMock()
            def setFocus(self): pass
            def selectAll(self): pass
            def text(self): return ""
            def setText(self, t): pass
            def clear(self): pass
            def setCompleter(self, c): pass
            def cursorPosition(self): return 0
            def setCursorPosition(self, p): pass
        qtwidgets.QLineEdit = _QLineEdit

    if not hasattr(qtwidgets, "QCompleter"):
        class _QCompleter:
            PopupCompletion = 0
            def __init__(self, *a, **kw): pass
            def setCaseSensitivity(self, v): pass
            def setCompletionMode(self, m): pass
            @property
            def activated(self): return MagicMock()
        qtwidgets.QCompleter = _QCompleter

    if not hasattr(qtcore, "QStringListModel"):
        class _QStringListModel:
            def __init__(self, *a, **kw): pass
            def setStringList(self, lst): pass
        qtcore.QStringListModel = _QStringListModel

    # Qt namespace constants
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


# ---------------------------------------------------------------------------
# Lightweight stand-in for ThumbnailViewWidget filter state.
# ---------------------------------------------------------------------------

def _make_filter_view(all_files=None, is_loading=False, service=None):
    """Build a minimal stand-in with filter state and methods from ThumbnailViewWidget."""
    from gui.thumbnail_view import ThumbnailViewWidget

    from gui.thumbnail_model import ThumbnailModel
    view = object.__new__(ThumbnailViewWidget)
    view.model = ThumbnailModel()

    view.model.all_files = list(all_files or [])
    view.model.all_files_set = set(view.model.all_files)
    view.model.current_files = list(view.model.all_files)
    view._is_loading = is_loading
    view._folder_is_cached = False

    view.model.current_filter = ""
    view.model.current_star_filter = [True, True, True, True, True, True]
    view.model.current_tag_filter = []
    view.model.clip_search_paths = None
    view.model.person_filter_paths = None
    view.model.hidden_indices = set()
    view.model.visible_to_original = {}
    view.model.original_to_visible = {}
    view.model.visible_original_indices = []
    view.model.last_layout_file_count = 0

    view._virtual_grid = None
    view.labels = {}
    view.service = service

    view.filtersApplied = MagicMock()
    view._viewport_executor = MagicMock()
    view.model.image_states = {}

    # FilterController — use the real class so filter logic is tested
    from gui.filter_controller import FilterController
    fc = object.__new__(FilterController)
    fc.model = view.model
    fc._executor = view._viewport_executor
    fc.service = service
    fc._filter_in_flight = False
    fc._filter_pending = False
    fc.needs_heatmap_seed = False
    fc._filter_update_timer = MagicMock()
    fc._filtered_paths_ready = MagicMock()
    fc.filters_applied = MagicMock()
    fc._is_loading = lambda: view._is_loading
    fc._label_count = lambda: len(view.labels)
    fc._on_layout_rebuilt = view._rebuild_layout_for_filter
    view.filter_controller = fc

    return view


def _insert_with_rating(db, file_path, rating):
    """Insert a DB record with a specific rating via direct SQL (no filesystem)."""
    import hashlib, time as _time
    path_hash = hashlib.md5(file_path.encode()).hexdigest()
    now = _time.time()
    with db.images._lock:
        db.images.conn.execute(
            "INSERT OR REPLACE INTO image_metadata "
            "(file_path, path_hash, rating, file_size, mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0, ?, ?)",
            (file_path, path_hash, rating, now, now),
        )
        db.images.conn.commit()


# ===================================================================
# Bug 1: _is_loading cleared for cached folders
# ===================================================================

class TestIsLoadingCachedFolder:

    def test_cached_folder_clears_is_loading(self):
        """_on_initial_files_received sets _is_loading=False when files come from DB cache."""
        from gui.thumbnail_view import ThumbnailViewWidget

        view = _make_filter_view(is_loading=True)
        view._startup_t0 = None
        view._startup_first_scan_progress = False
        view._startup_inline_thumb_count = 0
        view.model.initial_thumb_paths = {}
        view.model.path_to_idx = {}
        view.model.pixmap_cache = {}

        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._on_initial_files_received(view, files)

        assert view._folder_is_cached is True
        assert view._is_loading is False

    def test_empty_folder_keeps_is_loading(self):
        """_on_initial_files_received with no files (new folder) keeps _is_loading=True."""
        from gui.thumbnail_view import ThumbnailViewWidget

        view = _make_filter_view(is_loading=True)
        view._startup_t0 = None
        view._startup_first_scan_progress = False
        view._startup_inline_thumb_count = 0
        view.model.initial_thumb_paths = {}
        view.model.path_to_idx = {}
        view.model.pixmap_cache = {}

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget._on_initial_files_received(view, [])

        assert view._folder_is_cached is False
        assert view._is_loading is True

    def test_cached_folder_reapply_uses_daemon_path(self):
        """After cached folder clears _is_loading, reapply_filters submits to the daemon."""
        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, is_loading=False, service=mock_client)
        fc = view.filter_controller

        with patch("gui.filter_controller.event_system"):
            fc.reapply_filters()

        assert fc._filter_in_flight is True
        fc._executor.submit.assert_called_once()

    def test_is_loading_true_takes_fast_path(self):
        """When _is_loading is True, reapply_filters shows all files (no daemon query)."""
        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, is_loading=True, service=mock_client)
        view.model.current_star_filter = [False, False, True, False, False, False]
        fc = view.filter_controller

        with patch("gui.filter_controller.event_system"):
            fc.reapply_filters()

        fc._executor.submit.assert_not_called()
        assert fc._filter_in_flight is False


# ===================================================================
# Bug 2: clear_filter() method
# ===================================================================

class TestClearFilter:

    def test_clear_filter_resets_text(self):
        view = _make_filter_view()
        view.model.current_filter = "sunset"
        view.filter_controller.clear_filter()
        assert view.model.current_filter == ""

    def test_clear_filter_resets_star_states(self):
        view = _make_filter_view()
        view.model.current_star_filter = [False, False, True, False, False, False]
        view.filter_controller.clear_filter()
        assert view.model.current_star_filter == [True, True, True, True, True, True]

    def test_clear_filter_resets_hidden_indices(self):
        view = _make_filter_view()
        view.model.hidden_indices = {0, 3, 7}
        view.filter_controller.clear_filter()
        assert view.model.hidden_indices == set()

    def test_clear_filter_starts_debounce_timer(self):
        view = _make_filter_view()
        view.model.current_filter = "test"
        view.model.current_star_filter = [False] * 6
        view.filter_controller.clear_filter()
        view.filter_controller._filter_update_timer.start.assert_called_once()


# ===================================================================
# Filter in-flight / pending queuing
# ===================================================================

class TestFilterQueuing:

    def test_second_call_queues_pending(self):
        mock_client = MagicMock()
        files = ["/img/a.jpg"]
        view = _make_filter_view(all_files=files, service=mock_client)
        fc = view.filter_controller
        fc._filter_in_flight = True

        with patch("gui.filter_controller.event_system"):
            fc.reapply_filters()

        assert fc._filter_pending is True
        fc._executor.submit.assert_not_called()

    def test_pending_resubmits_after_completion(self):
        mock_client = MagicMock()
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files, service=mock_client)
        fc = view.filter_controller
        fc._filter_in_flight = True
        fc._filter_pending = True

        with patch("gui.filter_controller.event_system"):
            fc._on_filtered_paths_ready(set(files))

        assert fc._filter_in_flight is True
        assert fc._filter_pending is False
        fc._executor.submit.assert_called_once()


# ===================================================================
# apply_filter / apply_star_filter
# ===================================================================

class TestApplyFilter:

    def test_apply_filter_sets_text(self):
        view = _make_filter_view()
        fc = view.filter_controller
        fc.apply_filter("beach")
        assert view.model.current_filter == "beach"
        fc._filter_update_timer.start.assert_called()

    def test_apply_star_filter_sets_states(self):
        view = _make_filter_view()
        fc = view.filter_controller
        new_states = [True, False, True, False, True, False]
        fc.apply_star_filter(new_states)
        assert view.model.current_star_filter == new_states
        fc._filter_update_timer.start.assert_called()


# ===================================================================
# _apply_filter_results hides correct indices
# ===================================================================

class TestApplyFilterResults:

    def test_hides_non_visible_paths(self):
        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg", "/img/d.jpg"]
        view = _make_filter_view(all_files=files)
        fc = view.filter_controller
        visible = {"/img/a.jpg", "/img/c.jpg"}
        with patch("gui.filter_controller.event_system"):
            fc._apply_filter_results(visible)
        assert view.model.hidden_indices == {1, 3}

    def test_all_visible_clears_hidden(self):
        files = ["/img/a.jpg", "/img/b.jpg"]
        view = _make_filter_view(all_files=files)
        fc = view.filter_controller
        view.model.hidden_indices = {0, 1}
        with patch("gui.filter_controller.event_system"):
            fc._apply_filter_results(set(files))
        assert view.model.hidden_indices == set()

    def test_none_visible_hides_all(self):
        files = ["/img/a.jpg", "/img/b.jpg", "/img/c.jpg"]
        view = _make_filter_view(all_files=files)
        fc = view.filter_controller
        with patch("gui.filter_controller.event_system"):
            fc._apply_filter_results(set())
        assert view.model.hidden_indices == {0, 1, 2}


# ===================================================================
# Database filtering (get_filtered_file_paths)
# ===================================================================

class TestDatabaseFiltering:

    def test_all_stars_enabled_returns_everything(self, tmp_env):
        db = tmp_env["db"]
        for path, rating in [("/img/a.jpg", 0), ("/img/b.jpg", 3), ("/img/c.jpg", 5)]:
            _insert_with_rating(db, path, rating)

        result = db.images.get_filtered_file_paths("", [True] * 6)
        assert set(result) == {"/img/a.jpg", "/img/b.jpg", "/img/c.jpg"}

    def test_filter_by_rating(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/unrated.jpg", 0)
        _insert_with_rating(db, "/img/good.jpg", 4)
        _insert_with_rating(db, "/img/best.jpg", 5)

        stars = [False, False, False, False, True, True]
        result = db.images.get_filtered_file_paths("", stars)
        assert set(result) == {"/img/good.jpg", "/img/best.jpg"}

    def test_no_stars_returns_empty(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/a.jpg", 3)

        result = db.images.get_filtered_file_paths("", [False] * 6)
        assert result == []

    def test_text_filter(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/photos/beach.jpg", 0)
        _insert_with_rating(db, "/photos/mountain.jpg", 0)

        result = db.images.get_filtered_file_paths("beach", [True] * 6)
        assert result == ["/photos/beach.jpg"]

    def test_combined_text_and_star_filter(self, tmp_env):
        db = tmp_env["db"]
        _insert_with_rating(db, "/img/beach_good.jpg", 4)
        _insert_with_rating(db, "/img/beach_ok.jpg", 2)
        _insert_with_rating(db, "/img/mountain.jpg", 4)

        stars = [False, False, False, False, True, False]
        result = db.images.get_filtered_file_paths("beach", stars)
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
                # Closure captures `dialog` by name — safe because side_effect
                # is called after dialog is assigned on the line below MockBtn.
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
