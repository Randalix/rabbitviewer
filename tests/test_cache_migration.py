"""Tests for the CACHE_VERSION migration in ThumbnailManager."""

import os
import pytest
from unittest.mock import MagicMock, patch

from core.thumbnail_manager import ThumbnailManager, CACHE_VERSION


@pytest.fixture
def tmp_env(tmp_path):
    """Minimal ThumbnailManager environment with real dirs and a mock DB."""
    cache_dir = str(tmp_path / "cache")
    thumb_dir = os.path.join(cache_dir, "thumbnails")
    image_dir = os.path.join(cache_dir, "images")
    os.makedirs(thumb_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)

    # Seed some fake cached files
    (tmp_path / "cache" / "thumbnails" / "aaa.jpg").write_bytes(b"\xff")
    (tmp_path / "cache" / "images" / "bbb.jpg").write_bytes(b"\xff")

    db = MagicMock()
    db.clear_all_thumbnail_paths.return_value = 42

    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "cache_dir": cache_dir,
        "thumbnail_size": 128,
        "min_file_size": 8192,
        "ignore_patterns": [],
        "fullres_mem_cache_mb": 0,
        "fullres_cache_threshold_ms": 500,
    }.get(key, default)

    return {
        "cache_dir": cache_dir,
        "thumb_dir": thumb_dir,
        "image_dir": image_dir,
        "db": db,
        "config": config,
    }


def _make_tm(env):
    """Construct a ThumbnailManager without starting workers."""
    tm = ThumbnailManager.__new__(ThumbnailManager)
    tm.config_manager = env["config"]
    tm.metadata_db = env["db"]
    tm.cache_dir = env["cache_dir"]
    tm.thumbnail_cache_dir = env["thumb_dir"]
    tm.image_cache_dir = env["image_dir"]
    return tm


class TestCacheMigration:
    def test_first_run_clears_cache(self, tmp_env):
        """No version file → migration runs, clears DB and files."""
        tm = _make_tm(tmp_env)
        tm._check_cache_migration()

        tmp_env["db"].images.clear_all_thumbnail_paths.assert_called_once()
        assert not os.listdir(tmp_env["thumb_dir"])
        assert not os.listdir(tmp_env["image_dir"])

        version_file = os.path.join(tmp_env["cache_dir"], "cache_version")
        assert os.path.exists(version_file)
        with open(version_file) as f:
            assert int(f.read().strip()) == CACHE_VERSION

    def test_current_version_skips(self, tmp_env):
        """Version file matches CACHE_VERSION → no migration."""
        version_file = os.path.join(tmp_env["cache_dir"], "cache_version")
        with open(version_file, "w") as f:
            f.write(str(CACHE_VERSION))

        tm = _make_tm(tmp_env)
        tm._check_cache_migration()

        tmp_env["db"].images.clear_all_thumbnail_paths.assert_not_called()
        # Files should still be present
        assert os.listdir(tmp_env["thumb_dir"])

    def test_old_version_triggers_migration(self, tmp_env):
        """Version file < CACHE_VERSION → migration runs."""
        version_file = os.path.join(tmp_env["cache_dir"], "cache_version")
        with open(version_file, "w") as f:
            f.write("0")

        tm = _make_tm(tmp_env)
        tm._check_cache_migration()

        tmp_env["db"].images.clear_all_thumbnail_paths.assert_called_once()
        assert not os.listdir(tmp_env["thumb_dir"])

    def test_idempotent(self, tmp_env):
        """Running migration twice: second run is a no-op."""
        tm = _make_tm(tmp_env)
        tm._check_cache_migration()
        tmp_env["db"].images.clear_all_thumbnail_paths.reset_mock()

        # Re-seed a file to verify it's not deleted
        with open(os.path.join(tmp_env["thumb_dir"], "new.jpg"), "wb") as f:
            f.write(b"\xff")

        tm._check_cache_migration()
        tmp_env["db"].images.clear_all_thumbnail_paths.assert_not_called()
        assert os.path.exists(os.path.join(tmp_env["thumb_dir"], "new.jpg"))


class TestClearAllThumbnailPaths:
    def test_bulk_clear(self, tmp_path):
        """clear_all_thumbnail_paths NULLs all thumbnail/view_image paths."""
        from core.metadata_database import MetadataDatabase
        db = MetadataDatabase(str(tmp_path / "test.db"))

        # Create real files so set_thumbnail_paths can stat them
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        a.write_bytes(b"\xff\xd8")
        b.write_bytes(b"\xff\xd8")

        db.images.set_thumbnail_paths(str(a), thumbnail_path="/cache/a_thumb.jpg")
        db.images.set_thumbnail_paths(str(b), thumbnail_path="/cache/b_thumb.jpg",
                               view_image_path="/cache/b_view.jpg")

        rows = db.images.clear_all_thumbnail_paths()
        assert rows == 2

        paths_a = db.images.get_thumbnail_paths(str(a))
        paths_b = db.images.get_thumbnail_paths(str(b))
        assert paths_a.get("thumbnail_path") is None
        assert paths_b.get("thumbnail_path") is None
        assert paths_b.get("view_image_path") is None

    def test_no_rows_returns_zero(self, tmp_path):
        from core.metadata_database import MetadataDatabase
        db = MetadataDatabase(str(tmp_path / "test.db"))
        assert db.images.clear_all_thumbnail_paths() == 0
