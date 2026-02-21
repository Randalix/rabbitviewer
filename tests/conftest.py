"""
Shared pytest fixtures for RabbitViewer tests.
"""
import os
import sys
import pytest
from PIL import Image

# Ensure project root is on path for all tests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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
