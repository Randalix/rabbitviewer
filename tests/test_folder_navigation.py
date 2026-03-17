"""Tests for folder navigation: FolderNode, DB subdirectory discovery, ThumbnailService facade."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.folder_node import FolderNode
from core.metadata_database import MetadataDatabase


# ---------------------------------------------------------------------------
# FolderNode dataclass
# ---------------------------------------------------------------------------

class TestFolderNode:

    def test_defaults(self):
        node = FolderNode(path="/photos/vacation", name="vacation")
        assert node.path == "/photos/vacation"
        assert node.name == "vacation"
        assert node.parent_path is None
        assert node.image_count == 0
        assert node.recursive_count == 0
        assert node.preview_paths == []

    def test_with_data(self):
        node = FolderNode(
            path="/photos/vacation",
            name="vacation",
            parent_path="/photos",
            image_count=5,
            recursive_count=42,
            preview_paths=["/cache/a.jpg", "/cache/b.jpg"],
        )
        assert node.parent_path == "/photos"
        assert node.image_count == 5
        assert node.recursive_count == 42
        assert len(node.preview_paths) == 2


# ---------------------------------------------------------------------------
# MetadataDatabase.get_subdirectory_info
# ---------------------------------------------------------------------------

def _insert_file(db: MetadataDatabase, file_path: str, thumbnail_path: str = None):
    """Insert a minimal image_metadata record."""
    now = time.time()
    with db.images._lock:
        cursor = db.images.conn.cursor()
        cursor.execute(
            """INSERT OR IGNORE INTO image_metadata
               (file_path, path_hash, thumbnail_path, mtime, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (file_path, file_path, thumbnail_path, now, now, now),
        )
        db.images.conn.commit()


class TestGetSubdirectoryInfo:

    def test_no_subdirectories(self, tmp_env):
        db = tmp_env["db"]
        # Files directly in /photos, no subdirectories
        _insert_file(db, "/photos/img1.jpg")
        _insert_file(db, "/photos/img2.jpg")

        result = db.images.get_subdirectory_info("/photos")
        assert result == []

    def test_single_subdirectory(self, tmp_env):
        db = tmp_env["db"]
        _insert_file(db, "/photos/vacation/img1.jpg", "/cache/t1.jpg")
        _insert_file(db, "/photos/vacation/img2.jpg", "/cache/t2.jpg")

        result = db.images.get_subdirectory_info("/photos")
        assert len(result) == 1
        assert result[0]["name"] == "vacation"
        assert result[0]["path"] == "/photos/vacation"
        assert result[0]["image_count"] == 2
        assert result[0]["recursive_count"] == 2
        assert len(result[0]["preview_paths"]) == 2

    def test_multiple_subdirectories_sorted(self, tmp_env):
        db = tmp_env["db"]
        _insert_file(db, "/photos/zebra/img1.jpg")
        _insert_file(db, "/photos/alpha/img1.jpg")
        _insert_file(db, "/photos/alpha/img2.jpg")

        result = db.images.get_subdirectory_info("/photos")
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "zebra"
        assert result[0]["image_count"] == 2
        assert result[1]["image_count"] == 1

    def test_nested_subdirectories_counts(self, tmp_env):
        db = tmp_env["db"]
        # /photos/vacation/day1/img1.jpg — nested two levels deep
        _insert_file(db, "/photos/vacation/img1.jpg")
        _insert_file(db, "/photos/vacation/day1/img2.jpg")
        _insert_file(db, "/photos/vacation/day1/img3.jpg")
        _insert_file(db, "/photos/vacation/day2/img4.jpg")

        result = db.images.get_subdirectory_info("/photos")
        assert len(result) == 1
        vacation = result[0]
        assert vacation["name"] == "vacation"
        assert vacation["image_count"] == 1       # only img1.jpg directly in /vacation
        assert vacation["recursive_count"] == 4   # all 4 files

    def test_preview_paths_limited_to_4(self, tmp_env):
        db = tmp_env["db"]
        for i in range(10):
            _insert_file(db, f"/photos/many/img{i}.jpg", f"/cache/t{i}.jpg")

        result = db.images.get_subdirectory_info("/photos")
        assert len(result[0]["preview_paths"]) == 4

    def test_preview_paths_skip_empty(self, tmp_env):
        db = tmp_env["db"]
        _insert_file(db, "/photos/mix/img1.jpg", "/cache/t1.jpg")
        _insert_file(db, "/photos/mix/img2.jpg", None)       # no thumbnail
        _insert_file(db, "/photos/mix/img3.jpg", "")         # empty string

        result = db.images.get_subdirectory_info("/photos")
        assert result[0]["preview_paths"] == ["/cache/t1.jpg"]

    def test_empty_database(self, tmp_env):
        db = tmp_env["db"]
        result = db.images.get_subdirectory_info("/photos")
        assert result == []

    def test_nested_navigation(self, tmp_env):
        """Navigating into a subdirectory should show its sub-subdirectories."""
        db = tmp_env["db"]
        _insert_file(db, "/photos/vacation/day1/img1.jpg")
        _insert_file(db, "/photos/vacation/day2/img2.jpg")
        _insert_file(db, "/photos/vacation/beach.jpg")

        # From /photos: one subdirectory "vacation"
        result = db.images.get_subdirectory_info("/photos")
        assert len(result) == 1
        assert result[0]["name"] == "vacation"

        # From /photos/vacation: two subdirectories "day1" and "day2"
        result = db.images.get_subdirectory_info("/photos/vacation")
        assert len(result) == 2
        assert result[0]["name"] == "day1"
        assert result[1]["name"] == "day2"
