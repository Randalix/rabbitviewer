"""
Shared pytest fixtures for RabbitViewer tests.
"""
import os
import sys
import types

# Ensure project root is on path for all tests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# PySide6 stubs — allow daemon/core modules to be imported without Qt installed.
# Only installed when PySide6 is absent; a real installation takes precedence.
# ---------------------------------------------------------------------------
if 'PySide6' not in sys.modules:
    class _Signal:
        """No-op descriptor mirroring the PySide6.QtCore.Signal API."""
        def __init__(self, *args): pass
        def __get__(self, obj, type=None): return self
        def emit(self, *args): pass
        def connect(self, *args): pass
        def disconnect(self, *args): pass

    class _QObject:
        def __init__(self, parent=None): pass

    class _QPointF:
        # why: RabbitViewer reads .x/.y as attributes; real QPointF exposes .x()/.y()
        # as methods. Extend to callables if method-call form is ever exercised in tests.
        def __init__(self, x=0.0, y=0.0): self.x = x; self.y = y

    class _Qt:
        CaseInsensitive = 1
        Key_Return = 0x01000004
        Key_Enter = 0x01000005
        Key_Tab = 0x01000001
        Key_Escape = 0x01000000
        Key_Up = 0x01000013
        Key_Down = 0x01000015

    _qtcore = types.ModuleType('PySide6.QtCore')
    _qtcore.QObject = _QObject        # type: ignore[attr-defined]
    _qtcore.Signal = _Signal          # type: ignore[attr-defined]
    _qtcore.QPointF = _QPointF        # type: ignore[attr-defined]
    _qtcore.Qt = _Qt                  # type: ignore[attr-defined]

    # Permissive stub class: any attribute access returns a no-op callable / nested stub.
    class _Stub:
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return _Stub()
        def __getattr__(self, name): return _Stub()
        def __bool__(self): return False

    # QtGui stubs — enough for overlay_renderers and thumbnail_view imports
    _qtgui = types.ModuleType('PySide6.QtGui')
    for _name in ('QPixmap', 'QImage', 'QColor', 'QMouseEvent', 'QKeyEvent',
                   'QCursor', 'QPainter', 'QFont', 'QPainterPath', 'QPen'):
        setattr(_qtgui, _name, type(_name, (_Stub,), {}))

    # QtWidgets stubs
    _qtwidgets = types.ModuleType('PySide6.QtWidgets')
    # QWidget needs real stub methods for super() calls in subclass tests
    class _QWidget(_Stub):
        def mousePressEvent(self, e): pass
        def mouseReleaseEvent(self, e): pass
        def mouseMoveEvent(self, e): pass
        def mouseDoubleClickEvent(self, e): pass
        def paintEvent(self, e): pass
        def keyPressEvent(self, e): pass
        def keyReleaseEvent(self, e): pass
        def resizeEvent(self, e): pass
        def showEvent(self, e): pass
        def hideEvent(self, e): pass
        def closeEvent(self, e): pass
        def event(self, e): return False
        def update(self): pass
        def repaint(self): pass
        def setLayout(self, l): pass
        def setStyleSheet(self, s): pass
        def setFixedSize(self, *a): pass
        def setMouseTracking(self, b): pass
        def installEventFilter(self, f): pass

    _qtwidgets.QWidget = _QWidget
    for _name in ('QLabel', 'QScrollArea', 'QFrame', 'QMainWindow'):
        setattr(_qtwidgets, _name, type(_name, (_QWidget,), {}))
    for _name in ('QVBoxLayout', 'QGridLayout', 'QHBoxLayout'):
        setattr(_qtwidgets, _name, type(_name, (_Stub,), {
            'addWidget': lambda self, *a, **kw: None,
            'addLayout': lambda self, *a, **kw: None,
            'setContentsMargins': lambda self, *a: None,
            'setSpacing': lambda self, *a: None,
        }))
    setattr(_qtwidgets, 'QApplication', type('QApplication', (_Stub,), {}))
    class _QLineEdit(_QWidget):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._text = ""
            self._cursor = 0
        def text(self): return self._text
        def setText(self, t): self._text = t; self._cursor = len(t)
        def cursorPosition(self): return self._cursor
        def setCursorPosition(self, p): self._cursor = p
        def setPlaceholderText(self, t): pass
        def clear(self): self._text = ""; self._cursor = 0
    _qtwidgets.QLineEdit = _QLineEdit  # type: ignore[attr-defined]
    for _name in ('QTextEdit', 'QPlainTextEdit', 'QComboBox', 'QSpinBox', 'QDialog'):
        setattr(_qtwidgets, _name, type(_name, (_QWidget,), {}))
    # QCompleter needs class-level attributes (PopupCompletion, etc.)
    class _QCompleter(_Stub):
        PopupCompletion = 0
        def popup(self): return _Stub()
    _qtwidgets.QCompleter = _QCompleter  # type: ignore[attr-defined]
    _qtcore.QStringListModel = type('QStringListModel', (_Stub,), {})  # type: ignore[attr-defined]

    _pyside6 = types.ModuleType('PySide6')
    _pyside6.QtCore = _qtcore         # type: ignore[attr-defined]
    _pyside6.QtGui = _qtgui           # type: ignore[attr-defined]
    _pyside6.QtWidgets = _qtwidgets   # type: ignore[attr-defined]
    sys.modules['PySide6'] = _pyside6
    sys.modules['PySide6.QtCore'] = _qtcore
    sys.modules['PySide6.QtGui'] = _qtgui
    sys.modules['PySide6.QtWidgets'] = _qtwidgets

# ---------------------------------------------------------------------------
# watchdog stubs — allow filewatcher/network modules to import without watchdog
# ---------------------------------------------------------------------------
if 'watchdog' not in sys.modules:
    _wd = types.ModuleType('watchdog')
    _wd_observers = types.ModuleType('watchdog.observers')
    _wd_events = types.ModuleType('watchdog.events')

    class _Observer:
        def __init__(self): self._alive = False
        def is_alive(self): return self._alive
        def schedule(self, *a, **kw): pass
        def start(self): self._alive = True
        def stop(self): self._alive = False
        def join(self, timeout=None): pass

    class _FileSystemEventHandler:
        pass

    _wd_observers.Observer = _Observer           # type: ignore[attr-defined]
    _wd_events.FileSystemEventHandler = _FileSystemEventHandler  # type: ignore[attr-defined]

    sys.modules['watchdog'] = _wd
    sys.modules['watchdog.observers'] = _wd_observers
    sys.modules['watchdog.events'] = _wd_events

# ---------------------------------------------------------------------------
# send2trash stub — raises OSError for non-existent paths (realistic behaviour)
# ---------------------------------------------------------------------------
if 'send2trash' not in sys.modules:
    _s2t = types.ModuleType('send2trash')

    def _send2trash(path):
        if not os.path.exists(path):
            raise OSError(f"[Errno 2] No such file or directory: '{path}'")
        os.remove(path)

    _s2t.send2trash = _send2trash  # type: ignore[attr-defined]
    sys.modules['send2trash'] = _s2t

import pytest
from PIL import Image

import core.metadata_database as _mdb_module
from core.metadata_database import MetadataDatabase


class MockConfigManager:
    """Minimal ConfigManager substitute that accepts a plain dict.

    Only implements the interface used by ThumbnailManager and MetadataDatabase.
    """

    def __init__(self, overrides: dict | None = None):
        self._cfg: dict = {
            "thumbnail_size": 128,
            "min_file_size": 0,   # accept all file sizes in tests
            "ignore_patterns": [],
            "cache_dir": None,    # must be overridden per fixture
        }
        if overrides:
            self._cfg.update(overrides)

    def get(self, key: str, default=None):
        keys = key.split(".")
        val = self._cfg
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default


@pytest.fixture()
def tmp_env(tmp_path):
    """Clean, isolated test environment with a fresh database.

    Yields a dict with:
      tmp_path   — pathlib.Path temp directory (unique per test)
      cache_dir  — pathlib.Path cache subdirectory
      db_path    — str path to the SQLite database
      db         — MetadataDatabase instance
      config     — MockConfigManager configured for this environment

    The global MetadataDatabase singleton is reset before and after each test
    so tests never share state through the module-level singleton.
    """
    _mdb_module._metadata_database = None

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    db_path = str(tmp_path / "metadata.db")

    db = MetadataDatabase(db_path)
    config = MockConfigManager({"cache_dir": str(cache_dir)})

    yield {
        "tmp_path": tmp_path,
        "cache_dir": cache_dir,
        "db_path": db_path,
        "db": db,
        "config": config,
    }

    db.close()
    _mdb_module._metadata_database = None


@pytest.fixture()
def sample_images(tmp_env):
    """Creates 20 small JPEG images inside tmp_env and returns their paths."""
    img_dir = tmp_env["tmp_path"] / "images"
    img_dir.mkdir()
    paths: list[str] = []
    for i in range(20):
        path = img_dir / f"image_{i:04d}.jpg"
        color = (i * 12 % 255, i * 7 % 255, i * 3 % 255)
        Image.new("RGB", (800, 600), color=color).save(str(path), "JPEG")
        paths.append(str(path))
    return paths
