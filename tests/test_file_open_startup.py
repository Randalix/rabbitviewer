# tests/test_file_open_startup.py
"""Tests for macOS QFileOpenEvent handling during and after startup.

Covers the race where macOS delivers a QFileOpenEvent before the service
is ready (window._pending_file_open queuing) and that the queued path is
opened once _handle_startup_task fires.
"""
import os
import sys
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Qt stubs needed by main_window.py (safe — all use `if not hasattr` guards)
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    qtcore = sys.modules["PySide6.QtCore"]
    qtwidgets = sys.modules["PySide6.QtWidgets"]

    if not hasattr(qtcore, "Slot"):
        qtcore.Slot = lambda *a, **kw: (lambda fn: fn)

    if not hasattr(qtcore, "QTimer"):
        class _QTimer:
            def __init__(self, *a, **kw): pass
            def setSingleShot(self, v): pass
            def setInterval(self, v): pass
            def start(self, *a): pass
            def stop(self): pass
            def isActive(self): return False
            @property
            def timeout(self): return MagicMock()
            @staticmethod
            def singleShot(ms, cb): cb()
        qtcore.QTimer = _QTimer
    elif not hasattr(qtcore.QTimer, "singleShot"):
        qtcore.QTimer.singleShot = staticmethod(lambda ms, cb: cb())

    if not hasattr(qtcore, "QSettings"):
        class _QSettings:
            def __init__(self, *a, **kw): pass
            def value(self, k, default=None): return default
            def setValue(self, k, v): pass
        qtcore.QSettings = _QSettings

    if not hasattr(qtcore, "QEvent"):
        class _QEvent:
            class Type:
                FileOpen = 116
                KeyPress = 6
        qtcore.QEvent = _QEvent
    else:
        if not hasattr(qtcore.QEvent, "Type"):
            class _Type:
                FileOpen = 116
                KeyPress = 6
            qtcore.QEvent.Type = _Type
        elif not hasattr(qtcore.QEvent.Type, "FileOpen"):
            qtcore.QEvent.Type.FileOpen = 116

    if not hasattr(qtwidgets, "QMainWindow"):
        class _QMainWindow:
            def __init__(self, parent=None): pass
            def setWindowTitle(self, t): pass
            def setAcceptDrops(self, v): pass
            def resize(self, *a): pass
            def restoreGeometry(self, g): pass
            def setStyleSheet(self, s): pass
        qtwidgets.QMainWindow = _QMainWindow

    if not hasattr(qtwidgets, "QStackedWidget"):
        class _QStackedWidget:
            def __init__(self, *a, **kw): pass
            def addWidget(self, w): pass
            def setCurrentWidget(self, w): pass
            def currentWidget(self): return None
        qtwidgets.QStackedWidget = _QStackedWidget


_ensure_qt_stubs()


# ---------------------------------------------------------------------------
# Import MainWindow with temporary sys.modules stubs, then restore
# ---------------------------------------------------------------------------

_GUI_STUB_MODS = [
    "gui.thumbnail_view",
    "gui.hotkey_manager",
    "gui.metadata_cache",
    "gui.info_panel",
    "gui.filter_dialog",
    "gui.rating_filter_dialog",
    "gui.name_filter_dialog",
    "gui.tag_editor_dialog",
    "gui.tag_filter_dialog",
    "gui.date_filter_dialog",
    "gui.modal_menu",
    "gui.hotkey_help_overlay",
    "gui.menu_registry",
    "gui.status_bar",
    "network.gui_server",
    "network.daemon_signals",
    "scripts.script_manager",
]


def _import_main_window():
    """Import MainWindow with all heavy sub-modules stubbed, then restore sys.modules."""
    _MISSING = object()
    saved = {}
    for mod_name in _GUI_STUB_MODS:
        saved[mod_name] = sys.modules.get(mod_name, _MISSING)
        if sys.modules.get(mod_name, _MISSING) is _MISSING:
            sys.modules[mod_name] = MagicMock()
    saved["gui.main_window"] = sys.modules.pop("gui.main_window", _MISSING)

    try:
        import gui.main_window as _mw
        cls = _mw.MainWindow
    finally:
        # Restore all sub-modules (so later tests see the real packages)
        for mod_name, orig in saved.items():
            if mod_name == "gui.main_window":
                continue  # keep the imported module cached for this session
            if orig is _MISSING:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = orig

    return cls


MainWindow = _import_main_window()

from PySide6.QtCore import QEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_window():
    """Return a minimal MainWindow-like object without calling __init__."""
    w = object.__new__(MainWindow)
    w.service = None
    w._pending_file_open = None
    w.load_directory = MagicMock()
    w.open_media_view = MagicMock()
    w.thumbnail_view = MagicMock()
    w.metadata_cache = MagicMock()
    return w


def _fire_startup_service(window, target_dir=None, target_file=None, recursive=True):
    """Invoke _handle_startup_task('service', ...) with a fake service result."""
    fake_service = MagicMock()
    result = {
        "service": fake_service,
        "target_dir": target_dir,
        "target_file": target_file,
        "recursive": recursive,
    }
    with patch.object(
        sys.modules["PySide6.QtCore"].QTimer, "singleShot",
        side_effect=lambda ms, cb: cb()
    ):
        MainWindow._handle_startup_task(window, "service", result)
    return fake_service


def _run_file_open_handler(window, path, supported_exts=None):
    """Simulate the _file_open_handler closure from main._run_gui."""
    evt = MagicMock()
    evt.type.return_value = QEvent.Type.FileOpen
    evt.file.return_value = path

    if evt.type() == QEvent.Type.FileOpen:
        p = evt.file()
        if p and os.path.isfile(p):
            svc = window.service
            if not svc:
                window._pending_file_open = p
                return True
            exts = supported_exts or {".jpg", ".jpeg", ".png", ".tiff", ".cr3"}
            _, ext = os.path.splitext(p)
            if not ext or ext.lower() not in exts:
                return False
            window.load_directory(os.path.dirname(p), recursive=False)
            window.open_media_view(p)
            return True
    return False


# ===========================================================================
# Event handler: queuing when service is not ready
# ===========================================================================

class TestFileOpenHandlerQueuing:
    def test_queues_path_when_service_not_ready(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"fake")

        window = _make_window()
        result = _run_file_open_handler(window, str(img))

        assert result is True
        assert window._pending_file_open == str(img)
        window.load_directory.assert_not_called()
        window.open_media_view.assert_not_called()

    def test_opens_immediately_when_service_ready(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"fake")

        window = _make_window()
        window.service = MagicMock()
        result = _run_file_open_handler(window, str(img))

        assert result is True
        window.load_directory.assert_called_once_with(str(tmp_path), recursive=False)
        window.open_media_view.assert_called_once_with(str(img))
        assert window._pending_file_open is None

    def test_ignores_nonexistent_file(self, tmp_path):
        window = _make_window()
        result = _run_file_open_handler(window, str(tmp_path / "ghost.jpg"))

        assert result is False
        assert window._pending_file_open is None

    def test_ignores_unsupported_extension(self, tmp_path):
        exe = tmp_path / "prog.exe"
        exe.write_bytes(b"fake")

        window = _make_window()
        window.service = MagicMock()
        result = _run_file_open_handler(window, str(exe), supported_exts={".jpg", ".png"})

        assert result is False
        window.open_media_view.assert_not_called()

    def test_latest_queued_path_wins(self, tmp_path):
        """If two events arrive before service is ready, keep the last one."""
        img1 = tmp_path / "a.jpg"
        img2 = tmp_path / "b.jpg"
        img1.write_bytes(b"fake")
        img2.write_bytes(b"fake")

        window = _make_window()
        _run_file_open_handler(window, str(img1))
        _run_file_open_handler(window, str(img2))

        assert window._pending_file_open == str(img2)


# ===========================================================================
# _handle_startup_task: processes pending file once service is ready
# ===========================================================================

class TestHandleStartupTaskPendingFile:
    def test_opens_pending_file_when_no_target_dir(self, tmp_path):
        img = tmp_path / "pending.jpg"
        img.write_bytes(b"fake")

        window = _make_window()
        window._pending_file_open = str(img)

        _fire_startup_service(window)

        window.load_directory.assert_called_once_with(str(tmp_path), recursive=False)
        window.open_media_view.assert_called_once_with(str(img))

    def test_clears_pending_after_processing(self, tmp_path):
        img = tmp_path / "img.jpg"
        img.write_bytes(b"fake")

        window = _make_window()
        window._pending_file_open = str(img)
        _fire_startup_service(window)

        assert window._pending_file_open is None

    def test_cli_target_dir_takes_priority_over_pending(self, tmp_path):
        """CLI-supplied directory must win; pending file is not opened."""
        img = tmp_path / "pending.jpg"
        img.write_bytes(b"fake")
        cli_dir = str(tmp_path)

        window = _make_window()
        window._pending_file_open = str(img)

        _fire_startup_service(window, target_dir=cli_dir, recursive=True)

        window.load_directory.assert_called_once_with(cli_dir, True)
        # open_media_view not called (no target_file supplied via CLI)
        window.open_media_view.assert_not_called()

    def test_cli_target_file_opens_normally(self, tmp_path):
        img = tmp_path / "cli.jpg"
        img.write_bytes(b"fake")

        window = _make_window()
        _fire_startup_service(window, target_dir=str(tmp_path),
                               target_file=str(img), recursive=False)

        window.load_directory.assert_called_once_with(str(tmp_path), False)
        window.open_media_view.assert_called_once_with(str(img))

    def test_no_pending_no_target_dir_does_nothing(self):
        window = _make_window()
        _fire_startup_service(window)

        window.load_directory.assert_not_called()
        window.open_media_view.assert_not_called()

    def test_service_is_injected(self):
        window = _make_window()
        svc = _fire_startup_service(window)

        assert window.service is svc
        window.thumbnail_view.set_service.assert_called_once_with(svc)
        window.metadata_cache.set_service.assert_called_once_with(svc)
