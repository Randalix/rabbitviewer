"""
Shared pytest fixtures for RabbitViewer tests.
"""
import os
import sys
import types

# Ensure project root is on path for all tests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# pydantic stub — allow network/protocol.py to be imported without pydantic.
# Only installed when pydantic is absent; a real installation takes precedence.
# ---------------------------------------------------------------------------
if 'pydantic' not in sys.modules:
    _REQUIRED = object()  # sentinel for required fields with no default

    def _field_stub(*args, **kwargs):
        # why: Field(default_factory=...) passes no positional arg; return None so
        # _BaseModel.__init__ doesn't propagate _REQUIRED sentinel into instances.
        if args and args[0] is not ...:
            return args[0]
        if 'default' in kwargs:
            return kwargs['default']
        return _REQUIRED if not kwargs else None

    class _BaseModel:
        """Minimal pydantic.BaseModel stub for test environments."""
        def __init__(self, **kwargs):
            for klass in reversed(type(self).__mro__):
                for name, val in vars(klass).items():
                    if not name.startswith('_') and val is not _REQUIRED:
                        object.__setattr__(self, name, val)
            for k, v in kwargs.items():
                object.__setattr__(self, k, v)

        @classmethod
        def model_validate(cls, data: dict):
            return cls(**data)

        def model_dump(self) -> dict:
            return dict(self.__dict__)

    class _ValidationError(Exception):
        pass

    _pydantic = types.ModuleType('pydantic')
    _pydantic.BaseModel = _BaseModel          # type: ignore[attr-defined]
    _pydantic.Field = _field_stub             # type: ignore[attr-defined]
    _pydantic.ValidationError = _ValidationError  # type: ignore[attr-defined]
    sys.modules['pydantic'] = _pydantic

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
