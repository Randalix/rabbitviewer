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
        pass

    _qtcore = types.ModuleType('PySide6.QtCore')
    _qtcore.QObject = _QObject        # type: ignore[attr-defined]
    _qtcore.Signal = _Signal          # type: ignore[attr-defined]
    _qtcore.QPointF = _QPointF        # type: ignore[attr-defined]
    _qtcore.Qt = _Qt                  # type: ignore[attr-defined]

    _pyside6 = types.ModuleType('PySide6')
    _pyside6.QtCore = _qtcore         # type: ignore[attr-defined]
    sys.modules['PySide6'] = _pyside6
    sys.modules['PySide6.QtCore'] = _qtcore

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
