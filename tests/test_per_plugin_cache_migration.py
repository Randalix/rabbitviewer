"""Tests for per-plugin cache invalidation via BasePlugin.cache_version.

Validates _check_per_plugin_cache_migration() in ThumbnailManager: only rows
whose file extension matches the bumped plugin's supported formats get their
thumbnail_path/view_image_path cleared, and the on-disk JPEGs for those rows
are deleted. Other plugins' cached outputs are left alone.
"""
import json
import os
from unittest.mock import MagicMock

import pytest

from core.thumbnail_manager import ThumbnailManager


def _fake_plugin(name: str, exts: list[str], version: int):
    p = MagicMock()
    p.cache_version = version
    p.get_supported_formats.return_value = exts
    p.__class__.__name__ = name
    return p


@pytest.fixture
def tm_env(tmp_path):
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)

    db = MagicMock()

    tm = ThumbnailManager.__new__(ThumbnailManager)
    tm.cache_dir = cache_dir
    tm.metadata_db = db
    tm.plugin_registry = MagicMock()
    return {"tm": tm, "cache_dir": cache_dir, "db": db}


def test_missing_marker_invalidates_bumped_plugins(tm_env):
    """No marker file → treat missing entries as version 1 (default); plugins
    above default get invalidated, and the marker is written with current versions."""
    tm = tm_env["tm"]
    tm.plugin_registry.plugins = {
        "VideoPlugin": _fake_plugin("VideoPlugin", [".mp4"], 2),
        "PILPlugin": _fake_plugin("PILPlugin", [".jpg"], 1),
    }
    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.return_value = []

    tm._check_per_plugin_cache_migration()

    # VideoPlugin (v=2 > default 1) is invalidated; PILPlugin (v=1) is not.
    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.assert_called_once_with([".mp4"])
    marker = os.path.join(tm_env["cache_dir"], "plugin_cache_versions.json")
    assert os.path.exists(marker)
    with open(marker) as f:
        assert json.load(f) == {"VideoPlugin": 2, "PILPlugin": 1}


def test_fresh_install_all_default_is_noop(tm_env):
    """All plugins at default cache_version=1 and no marker → no clearing."""
    tm = tm_env["tm"]
    tm.plugin_registry.plugins = {
        "VideoPlugin": _fake_plugin("VideoPlugin", [".mp4"], 1),
        "PILPlugin": _fake_plugin("PILPlugin", [".jpg"], 1),
    }

    tm._check_per_plugin_cache_migration()

    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.assert_not_called()
    # Marker is still written so future bumps have a baseline.
    marker = os.path.join(tm_env["cache_dir"], "plugin_cache_versions.json")
    assert os.path.exists(marker)


def test_bump_clears_only_matching_plugin(tm_env, tmp_path):
    tm = tm_env["tm"]
    # Pre-seed marker at older versions.
    marker = os.path.join(tm_env["cache_dir"], "plugin_cache_versions.json")
    with open(marker, "w") as f:
        json.dump({"VideoPlugin": 1, "PILPlugin": 1}, f)

    tm.plugin_registry.plugins = {
        "VideoPlugin": _fake_plugin("VideoPlugin", [".mp4", ".mov"], 2),
        "PILPlugin": _fake_plugin("PILPlugin", [".jpg"], 1),
    }

    # Create real cached files that the migration should delete.
    video_thumb = tmp_path / "video_thumb.jpg"
    video_view = tmp_path / "video_view.jpg"
    video_thumb.write_bytes(b"\xff")
    video_view.write_bytes(b"\xff")

    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.return_value = [
        {
            "file_path": "/somewhere/clip.mp4",
            "thumbnail_path": str(video_thumb),
            "view_image_path": str(video_view),
        },
    ]

    tm._check_per_plugin_cache_migration()

    # Only the video plugin's extensions were cleared.
    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.assert_called_once_with(
        [".mp4", ".mov"]
    )
    # On-disk files were removed.
    assert not video_thumb.exists()
    assert not video_view.exists()
    # Marker updated to new versions.
    with open(marker) as f:
        assert json.load(f) == {"VideoPlugin": 2, "PILPlugin": 1}


def test_no_bump_is_noop(tm_env):
    tm = tm_env["tm"]
    marker = os.path.join(tm_env["cache_dir"], "plugin_cache_versions.json")
    with open(marker, "w") as f:
        json.dump({"VideoPlugin": 2, "PILPlugin": 1}, f)

    tm.plugin_registry.plugins = {
        "VideoPlugin": _fake_plugin("VideoPlugin", [".mp4"], 2),
        "PILPlugin": _fake_plugin("PILPlugin", [".jpg"], 1),
    }

    tm._check_per_plugin_cache_migration()

    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.assert_not_called()


def test_missing_disk_file_is_tolerated(tm_env):
    tm = tm_env["tm"]
    marker = os.path.join(tm_env["cache_dir"], "plugin_cache_versions.json")
    with open(marker, "w") as f:
        json.dump({"VideoPlugin": 1}, f)

    tm.plugin_registry.plugins = {
        "VideoPlugin": _fake_plugin("VideoPlugin", [".mp4"], 2),
    }
    tm.metadata_db.images.clear_thumbnail_paths_for_extensions.return_value = [
        {
            "file_path": "/x.mp4",
            "thumbnail_path": "/does/not/exist.jpg",
            "view_image_path": None,
        },
    ]

    # Must not raise.
    tm._check_per_plugin_cache_migration()
