"""Tests for bookmark copy/move workflow."""
import os
import threading
import time

import pytest

from core.bookmark_manager import (
    Bookmark,
    load_bookmarks,
    transfer_file,
    execute_bookmark_transfer,
    _should_transfer,
)
from core.metadata_database import MetadataDatabase
from core.rendermanager import RenderManager, Priority


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockConfigManager:
    def __init__(self, overrides=None):
        self._cfg = {
            "thumbnail_size": 128,
            "min_file_size": 0,
            "ignore_patterns": [],
            "cache_dir": None,
            "watch_paths": [],
        }
        if overrides:
            self._cfg.update(overrides)

    def get(self, key, default=None):
        keys = key.split(".")
        val = self._cfg
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default


@pytest.fixture()
def src_file(tmp_path):
    """Create a single source file for transfer tests."""
    src = tmp_path / "source" / "photo.jpg"
    src.parent.mkdir()
    src.write_bytes(b"\xff\xd8fake_jpeg_data")
    return src


@pytest.fixture()
def src_files(tmp_path):
    """Create multiple source files."""
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    paths = []
    for i in range(5):
        f = src_dir / f"img_{i:03d}.jpg"
        f.write_bytes(f"\xff\xd8fake_{i}".encode())
        paths.append(f)
    return paths


@pytest.fixture()
def dest_dir(tmp_path):
    return str(tmp_path / "dest")


@pytest.fixture()
def db(tmp_path):
    return MetadataDatabase(str(tmp_path / "test.db"))


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=2)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


@pytest.fixture()
def tm(tmp_path, rm):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    config = MockConfigManager({"cache_dir": str(cache_dir)})
    db = MetadataDatabase(str(tmp_path / "tm.db"))

    from core.thumbnail_manager import ThumbnailManager
    manager = ThumbnailManager(config, db, num_workers=2)
    manager.render_manager.shutdown(timeout=2)
    manager.render_manager = rm
    yield manager


# ===========================================================================
#  load_bookmarks
# ===========================================================================

class TestLoadBookmarks:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "core.bookmark_manager.BOOKMARKS_PATH",
            str(tmp_path / "nonexistent.yaml"),
        )
        assert load_bookmarks() == []

    def test_parses_valid_yaml(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "bookmarks.yaml"
        yaml_file.write_text(
            "bookmarks:\n"
            "  - key: a\n"
            "    name: Album\n"
            "    path: /photos/album\n"
            "  - key: b\n"
            "    path: ~/backup\n"
        )
        monkeypatch.setattr(
            "core.bookmark_manager.BOOKMARKS_PATH", str(yaml_file),
        )
        result = load_bookmarks()
        assert len(result) == 2
        assert result[0] == Bookmark(key="a", name="Album", path="/photos/album")
        assert result[1].name == os.path.expanduser("~/backup")

    def test_skips_entries_without_key(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "bookmarks.yaml"
        yaml_file.write_text(
            "bookmarks:\n"
            "  - name: NoKey\n"
            "    path: /somewhere\n"
        )
        monkeypatch.setattr(
            "core.bookmark_manager.BOOKMARKS_PATH", str(yaml_file),
        )
        assert load_bookmarks() == []

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "bookmarks.yaml"
        yaml_file.write_text("")
        monkeypatch.setattr(
            "core.bookmark_manager.BOOKMARKS_PATH", str(yaml_file),
        )
        assert load_bookmarks() == []


# ===========================================================================
#  _should_transfer (rsync-va skip semantics)
# ===========================================================================

class TestShouldTransfer:
    def test_transfers_when_dest_missing(self, src_file, tmp_path):
        dst = str(tmp_path / "dest" / "photo.jpg")
        assert _should_transfer(str(src_file), dst) is True

    def test_skips_when_identical(self, src_file, tmp_path):
        dst = tmp_path / "dest" / "photo.jpg"
        dst.parent.mkdir(parents=True)
        import shutil
        shutil.copy2(str(src_file), str(dst))
        assert _should_transfer(str(src_file), str(dst)) is False

    def test_transfers_when_size_differs(self, src_file, tmp_path):
        dst = tmp_path / "dest" / "photo.jpg"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"short")
        assert _should_transfer(str(src_file), str(dst)) is True

    def test_transfers_when_mtime_differs(self, src_file, tmp_path):
        dst = tmp_path / "dest" / "photo.jpg"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(src_file.read_bytes())
        # Set mtime 10 seconds apart
        os.utime(str(dst), (time.time() - 10, time.time() - 10))
        assert _should_transfer(str(src_file), str(dst)) is True


# ===========================================================================
#  transfer_file
# ===========================================================================

class TestTransferFile:
    def test_copy(self, src_file, dest_dir):
        result = transfer_file(str(src_file), dest_dir, move=False)
        assert result == "copied"
        assert src_file.exists()
        assert os.path.isfile(os.path.join(dest_dir, "photo.jpg"))

    def test_move(self, src_file, dest_dir):
        result = transfer_file(str(src_file), dest_dir, move=True)
        assert result == "moved"
        assert not src_file.exists()
        assert os.path.isfile(os.path.join(dest_dir, "photo.jpg"))

    def test_skip_identical(self, src_file, dest_dir):
        # First copy
        transfer_file(str(src_file), dest_dir, move=False)
        # Second copy should skip
        result = transfer_file(str(src_file), dest_dir, move=False)
        assert result == "skipped"

    def test_missing_source(self, dest_dir):
        result = transfer_file("/nonexistent/file.jpg", dest_dir, move=False)
        assert result.startswith("error:")

    def test_creates_dest_dir(self, src_file, tmp_path):
        nested = str(tmp_path / "a" / "b" / "c")
        result = transfer_file(str(src_file), nested, move=False)
        assert result == "copied"
        assert os.path.isfile(os.path.join(nested, "photo.jpg"))


# ===========================================================================
#  execute_bookmark_transfer
# ===========================================================================

class TestExecuteBookmarkTransfer:
    def test_copy_multiple(self, src_files, dest_dir):
        paths = [str(f) for f in src_files]
        result = execute_bookmark_transfer(paths, dest_dir, move=False)
        assert result["copied"] == 5
        assert result["skipped"] == 0
        assert result["errors"] == []
        for f in src_files:
            assert f.exists(), "copy should not remove source"

    def test_move_multiple(self, src_files, dest_dir):
        paths = [str(f) for f in src_files]
        result = execute_bookmark_transfer(paths, dest_dir, move=True)
        assert result["moved"] == 5
        for f in src_files:
            assert not f.exists(), "move should remove source"

    def test_idempotent_copy(self, src_file, dest_dir):
        path = str(src_file)
        r1 = execute_bookmark_transfer([path], dest_dir, move=False)
        r2 = execute_bookmark_transfer([path], dest_dir, move=False)
        assert r1["copied"] == 1
        assert r2["skipped"] == 1

    def test_with_db_tracking(self, src_files, dest_dir, db):
        paths = [str(f) for f in src_files]
        result = execute_bookmark_transfer(paths, dest_dir, move=False, db=db)
        assert result["copied"] == 5
        # All should be marked complete, none pending
        pending = db.ledgers.file_transfer_get_pending(dest_dir, "copy")
        assert pending == []

    def test_db_tracks_failures(self, dest_dir, db):
        paths = ["/nonexistent/a.jpg", "/nonexistent/b.jpg"]
        result = execute_bookmark_transfer(paths, dest_dir, move=False, db=db)
        assert len(result["errors"]) == 2
        # Failed transfers are marked complete with "failed" status, not pending
        pending = db.ledgers.file_transfer_get_pending(dest_dir, "copy")
        assert pending == []

    def test_mixed_success_and_failure(self, src_file, dest_dir, db):
        paths = [str(src_file), "/nonexistent/missing.jpg"]
        result = execute_bookmark_transfer(paths, dest_dir, move=False, db=db)
        assert result["copied"] == 1
        assert len(result["errors"]) == 1

    def test_without_db(self, src_file, dest_dir):
        result = execute_bookmark_transfer([str(src_file)], dest_dir, move=False, db=None)
        assert result["copied"] == 1

    def test_empty_list(self, dest_dir, db):
        result = execute_bookmark_transfer([], dest_dir, move=False, db=db)
        assert result["copied"] == 0
        assert result["errors"] == []


# ===========================================================================
#  Bookmark operations via ThumbnailManager (compound task pipeline)
# ===========================================================================

class TestBookmarkCompoundTask:
    def test_registry_contains_bookmark_ops(self, tm):
        assert tm.get_task_operation("bookmark_copy") is not None
        assert tm.get_task_operation("bookmark_move") is not None

    def test_op_bookmark_copy(self, tm, tmp_path):
        src = tmp_path / "src" / "a.jpg"
        src.parent.mkdir()
        src.write_bytes(b"\xff\xd8fake")
        dest = str(tmp_path / "bm_dest")

        result = tm._op_bookmark_copy([str(src)], dest_dir=dest)
        assert result["copied"] == 1
        assert src.exists()
        assert os.path.isfile(os.path.join(dest, "a.jpg"))

    def test_op_bookmark_move(self, tm, tmp_path):
        src = tmp_path / "src" / "b.jpg"
        src.parent.mkdir()
        src.write_bytes(b"\xff\xd8fake")
        dest = str(tmp_path / "bm_dest")

        result = tm._op_bookmark_move([str(src)], dest_dir=dest)
        assert result["moved"] == 1
        assert not src.exists()

    def test_bookmark_copy_via_compound_task(self, tm, tmp_path):
        src = tmp_path / "src" / "c.jpg"
        src.parent.mkdir()
        src.write_bytes(b"\xff\xd8fake")
        dest = str(tmp_path / "bm_dest")

        results = tm.execute_compound_task([
            ("bookmark_copy", [str(src)], {"dest_dir": dest}),
        ])
        assert results["bookmark_copy"]["copied"] == 1

    def test_bookmark_via_rendermanager(self, tm, rm, tmp_path):
        """Full async path: submit compound task through RenderManager."""
        src = tmp_path / "src" / "d.jpg"
        src.parent.mkdir()
        src.write_bytes(b"\xff\xd8fake")
        dest = str(tmp_path / "bm_dest")

        done = threading.Event()

        original = tm._op_bookmark_copy
        def _capture(paths, *, dest_dir):
            result = original(paths, dest_dir=dest_dir)
            done.set()
            return result

        tm._task_operations["bookmark_copy"] = _capture

        rm.submit_task(
            "test_bookmark::1",
            Priority.NORMAL,
            tm.execute_compound_task,
            [("bookmark_copy", [str(src)], {"dest_dir": dest})],
        )

        assert done.wait(timeout=5), "bookmark task did not execute"
        assert os.path.isfile(os.path.join(dest, "d.jpg"))
