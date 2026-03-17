"""Tests for selection preservation across layout changes.

Covers:
1. _recompute_selected_indices rebuilds index cache from path-based selection
2. remove_images preserves selection of surviving files
3. reorder_files preserves selection across index remaps
4. Deferred delete in watcher survives atomic-rename race
"""
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure Qt stubs are available
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    qtcore = sys.modules["PySide6.QtCore"]

    for attr in ("QPoint", "QSizeF", "QRectF", "QRect", "QSize"):
        if not hasattr(qtcore, attr):
            setattr(qtcore, attr, MagicMock)

    if not hasattr(qtcore, "Slot"):
        qtcore.Slot = lambda *a, **kw: (lambda fn: fn)

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

    if not hasattr(qtcore, "QEvent"):
        class _QEvent:
            Resize = 14
            def __init__(self, *a): pass
        qtcore.QEvent = _QEvent

    qtgui = sys.modules.get("PySide6.QtGui")
    if qtgui and not hasattr(qtgui, "QImage"):
        class _QImage:
            Format_RGBA8888 = 0
            def __init__(self, *a): pass
            def isNull(self): return True
        qtgui.QImage = _QImage

    qt = qtcore.Qt
    for attr, val in [("Window", 0), ("LeftButton", 1), ("RightButton", 2),
                      ("Dialog", 0), ("ArrowCursor", 0), ("CrossCursor", 0),
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

from gui.thumbnail_view import ThumbnailViewWidget
from gui.thumbnail_model import ImageState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_view(all_files, selected_paths=None):
    """Build a minimal ThumbnailViewWidget with selection state."""
    from gui.thumbnail_model import ThumbnailModel
    from gui.selection_interaction import SelectionInteraction
    view = object.__new__(ThumbnailViewWidget)
    view.model = ThumbnailModel()
    view.selection = SelectionInteraction(view.model, lambda idx, sel: None)

    view.model.all_files = list(all_files)
    view.model.all_files_set = set(view.model.all_files)
    view.model.current_files = list(view.model.all_files)

    view.model.current_filter = ""
    view.model.current_star_filter = [True] * 6
    view.model.current_tag_filter = []
    view.model.hidden_indices = set()
    view.model.visible_to_original = {}
    view.model.original_to_visible = {}
    view.model.visible_original_indices = []
    view.model.last_layout_file_count = len(view.model.all_files)

    view.model.path_to_idx = {}
    view.model.image_states = {}
    for i, f in enumerate(view.model.all_files):
        view.model.path_to_idx[f] = i
        view.model.image_states[i] = ImageState()
        view.model.visible_to_original[i] = i
        view.model.original_to_visible[i] = i
        view.model.visible_original_indices.append(i)

    # Selection state
    selected_paths = set(selected_paths or [])
    view.selection._current_selection = selected_paths
    view.selection._selected_indices = {view.model.path_to_idx[p] for p in selected_paths if p in view.model.path_to_idx}
    view.selection._last_preview_selected = set()
    view.selection._drag_start_index = -1
    view.selection._drag_last_index = -1
    view.selection._selection_mode = None
    view.selection.selection_anchor_index = None

    # Mocks for methods called by remove_images / reorder_files
    view._filter_in_flight = False
    view._filter_pending = False
    view._filter_update_timer = MagicMock()
    view._virtual_grid = MagicMock()
    view.model.initial_thumb_paths = {}
    view.model.thumb_path_cache = {}
    view.model.pixmap_cache = {}
    view.labels = {}
    view.service = MagicMock()
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
    view._scan_coalesce_timer = MagicMock()
    view._scan_batch_pending = False
    view._scan_first_batch_flushed = False
    view.model.scan_active = False
    view._sync_virtual_viewport = MagicMock()
    view._recycle_label = MagicMock()
    view.benchmarkComplete = MagicMock()
    view._is_loading = False
    view.model.group_mode = False
    view.model.grouped_raw_paths = set()
    view.model.raw_to_primary = {}
    view._last_redraw_time = 0

    return view


# ===================================================================
# _recompute_selected_indices
# ===================================================================

class TestRecomputeSelectedIndices:

    def test_basic_recompute(self):
        view = _make_view(["/a.jpg", "/b.jpg", "/c.jpg"], selected_paths=["/a.jpg", "/c.jpg"])
        # Manually corrupt indices to simulate stale state
        view.selection._selected_indices = {99, 100}
        view._recompute_selected_indices()
        assert view.selection._selected_indices == {0, 2}

    def test_recompute_drops_removed_paths(self):
        """Paths in _current_selection but not in _path_to_idx are silently dropped."""
        view = _make_view(["/a.jpg", "/b.jpg"])
        view.selection._current_selection = {"/a.jpg", "/gone.jpg"}
        view._recompute_selected_indices()
        assert view.selection._selected_indices == {0}

    def test_recompute_empty_selection(self):
        view = _make_view(["/a.jpg", "/b.jpg"])
        view.selection._current_selection = set()
        view._recompute_selected_indices()
        assert view.selection._selected_indices == set()


# ===================================================================
# remove_images — selection preservation
# ===================================================================

class TestRemoveImagesSelection:

    def test_removing_unselected_preserves_selection(self):
        """Removing files that are NOT selected must not clear the selection."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg", "/c.jpg"])

        published_commands = []
        with patch("gui.thumbnail_view.event_system") as mock_es:
            mock_es.publish = lambda cmd: published_commands.append(cmd)
            ThumbnailViewWidget.remove_images(view, ["/b.jpg"])

        # No selection command should be published (selection unchanged)
        selection_cmds = [c for c in published_commands if hasattr(c, 'paths')]
        assert len(selection_cmds) == 0

        # Indices should be recomputed for the new layout
        # After removing /b.jpg: /a.jpg=0, /c.jpg=1, /d.jpg=2
        assert view.selection._selected_indices == {0, 1}

    def test_removing_selected_file_updates_selection(self):
        """Removing a selected file should update selection to surviving paths."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg", "/b.jpg"])

        published_commands = []
        with patch("gui.thumbnail_view.event_system") as mock_es:
            mock_es.publish = lambda cmd: published_commands.append(cmd)
            ThumbnailViewWidget.remove_images(view, ["/b.jpg"])

        # A ReplaceSelectionCommand with surviving paths should be published
        selection_cmds = [c for c in published_commands if hasattr(c, 'paths')]
        assert len(selection_cmds) == 1
        assert selection_cmds[0].paths == {"/a.jpg"}

    def test_removing_all_selected_clears_selection(self):
        """Removing all selected files should publish an empty selection."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg", "/b.jpg"])

        published_commands = []
        with patch("gui.thumbnail_view.event_system") as mock_es:
            mock_es.publish = lambda cmd: published_commands.append(cmd)
            ThumbnailViewWidget.remove_images(view, ["/a.jpg", "/b.jpg"])

        selection_cmds = [c for c in published_commands if hasattr(c, 'paths')]
        assert len(selection_cmds) == 1
        assert selection_cmds[0].paths == set()

    def test_removing_nonexistent_preserves_selection(self):
        """Removing a path not in the view must not affect selection."""
        files = ["/a.jpg", "/b.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg"])

        published_commands = []
        with patch("gui.thumbnail_view.event_system") as mock_es:
            mock_es.publish = lambda cmd: published_commands.append(cmd)
            ThumbnailViewWidget.remove_images(view, ["/nonexistent.jpg"])

        selection_cmds = [c for c in published_commands if hasattr(c, 'paths')]
        assert len(selection_cmds) == 0


# ===================================================================
# reorder_files — selection preservation
# ===================================================================

class TestReorderFilesSelection:

    def test_reorder_preserves_selection(self):
        """Selection paths should survive an index remap via reorder_files."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg", "/c.jpg"])
        assert view.selection._selected_indices == {0, 2}

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget.reorder_files(view, ["/c.jpg", "/b.jpg", "/a.jpg"])

        # Paths unchanged, but indices should be remapped
        assert view.selection._current_selection == {"/a.jpg", "/c.jpg"}
        # After reverse sort: /c.jpg=0, /b.jpg=1, /a.jpg=2
        assert view.selection._selected_indices == {0, 2}

    def test_reorder_noop_preserves_selection(self):
        """If reorder is a no-op (same order), selection should be unchanged."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg"]
        view = _make_view(files, selected_paths=["/b.jpg"])
        assert view.selection._selected_indices == {1}

        with patch("gui.thumbnail_view.event_system"):
            ThumbnailViewWidget.reorder_files(view, ["/a.jpg", "/b.jpg", "/c.jpg"])

        # Same order → early return, indices unchanged
        assert view.selection._selected_indices == {1}

    def test_reorder_with_subset(self):
        """reorder_files with fewer paths drops unknown paths but preserves selection."""
        files = ["/a.jpg", "/b.jpg", "/c.jpg"]
        view = _make_view(files, selected_paths=["/a.jpg", "/c.jpg"])

        with patch("gui.thumbnail_view.event_system"):
            # /b.jpg dropped from ordered list
            ThumbnailViewWidget.reorder_files(view, ["/c.jpg", "/a.jpg"])

        assert view.selection._current_selection == {"/a.jpg", "/c.jpg"}
        # After reorder: /c.jpg=0, /a.jpg=1
        assert view.selection._selected_indices == {0, 1}


# ===================================================================
# Watcher deferred delete — atomic rename race
# ===================================================================

class TestDeferredDelete:

    def test_deferred_delete_file_exists_reindexes(self):
        """If file still exists after defer, treat as replacement (re-index)."""
        from filewatcher.watcher import WatchdogHandler

        tm = MagicMock()
        tm.create_tasks_for_file.return_value = []
        handler = WatchdogHandler(tm, [])

        # File exists → replacement, not deletion
        with patch("os.path.exists", return_value=True):
            handler._handle_deleted("/img/photo.jpg")

        tm.source_cache.invalidate.assert_called_once_with("/img/photo.jpg")
        tm.create_tasks_for_file.assert_called_once()
        # Should NOT notify files_removed
        tm.render_manager.notify.assert_not_called()

    def test_deferred_delete_file_gone_removes(self):
        """If file is truly gone after defer, remove from DB and notify GUI."""
        from filewatcher.watcher import WatchdogHandler

        tm = MagicMock()
        handler = WatchdogHandler(tm, [])

        with patch("os.path.exists", return_value=False):
            handler._handle_deleted("/img/photo.jpg")

        # Should submit db_cleanup_deleted
        call_args = tm.render_manager.submit_task.call_args
        assert "db_cleanup_deleted" in call_args[0][0]
        # Should notify files_removed
        tm.render_manager.notify.assert_called_once()

    def test_defer_allows_rename_to_complete(self):
        """Simulate atomic rename race: file missing at delete time, present after defer."""
        from filewatcher.watcher import WatchdogHandler

        tm = MagicMock()
        tm.create_tasks_for_file.return_value = []
        handler = WatchdogHandler(tm, [])

        # dispatch defers via Timer; we call _handle_deleted directly
        # simulating file existing after the defer window
        with patch("os.path.exists", return_value=True):
            handler._handle_deleted("/img/photo.jpg")

        # Treated as replacement: re-index, no removal notification
        tm.create_tasks_for_file.assert_called_once()
        tm.render_manager.notify.assert_not_called()
