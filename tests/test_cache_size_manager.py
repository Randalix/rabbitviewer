"""Tests for cache size management and LRU eviction."""

import os
import time
import pytest

from core.metadata_database import MetadataDatabase
from core.cache_size_manager import CacheSizeManager


@pytest.fixture()
def cache_env(tmp_path):
    """Provide a DB, thumbnail dir, and view-image dir for cache tests."""
    db_path = str(tmp_path / "test.db")
    thumb_dir = tmp_path / "thumbnails"
    img_dir = tmp_path / "images"
    thumb_dir.mkdir()
    img_dir.mkdir()

    db = MetadataDatabase(db_path)

    # Helper to create a fake cached image and register it in the DB.
    def _add_cached_file(name: str, thumb_kb: int = 10, view_kb: int = 50,
                         accessed_at: float = 0.0):
        src = tmp_path / name
        src.write_bytes(os.urandom(1024))  # dummy source

        thumb_file = thumb_dir / f"{name}.jpg"
        thumb_file.write_bytes(b"\x00" * (thumb_kb * 1024))

        view_file = img_dir / f"{name}_view.jpg"
        view_file.write_bytes(b"\x00" * (view_kb * 1024))

        # Insert the record — set_thumbnail_paths needs the source to exist for INSERT.
        db.set_thumbnail_paths(str(src), thumbnail_path=str(thumb_file),
                               view_image_path=str(view_file))

        # Manually set accessed_at for deterministic LRU ordering.
        with db._lock:
            db.conn.execute(
                "UPDATE image_metadata SET accessed_at = ? WHERE file_path = ?",
                (accessed_at, str(src)),
            )
            db.conn.commit()

        return str(src)

    return db, thumb_dir, img_dir, _add_cached_file


class TestGetTotalCacheSize:
    def test_empty_db(self, tmp_path):
        db = MetadataDatabase(str(tmp_path / "empty.db"))
        assert db.get_total_cache_size() == 0

    def test_sums_thumb_and_view(self, cache_env):
        db, thumb_dir, img_dir, add = cache_env
        add("a.jpg", thumb_kb=10, view_kb=20)
        add("b.jpg", thumb_kb=5, view_kb=15)

        total = db.get_total_cache_size()
        expected = (10 + 20 + 5 + 15) * 1024
        assert total == expected


class TestEvictLruCache:
    def test_no_eviction_when_under_target(self, cache_env):
        db, _, _, add = cache_env
        add("a.jpg", thumb_kb=10, view_kb=10)
        freed = db.evict_lru_cache(target_bytes=1_000_000)
        assert freed == 0

    def test_evicts_oldest_first(self, cache_env):
        db, thumb_dir, img_dir, add = cache_env
        src_old = add("old.jpg", thumb_kb=10, view_kb=40, accessed_at=100.0)
        src_new = add("new.jpg", thumb_kb=10, view_kb=40, accessed_at=200.0)

        total_before = db.get_total_cache_size()
        assert total_before == 100 * 1024  # (10+40)*2

        # Target: 60 KB — need to evict one record (50 KB) to get below.
        freed = db.evict_lru_cache(target_bytes=60 * 1024)
        assert freed > 0

        # The old record should have been evicted (paths are None).
        paths = db.get_thumbnail_paths(src_old)
        assert paths.get("thumbnail_path") is None

        # The new record should still exist.
        paths = db.get_thumbnail_paths(src_new)
        assert paths.get("thumbnail_path") is not None

    def test_evicts_multiple_records(self, cache_env):
        db, _, _, add = cache_env
        for i in range(5):
            add(f"img{i}.jpg", thumb_kb=10, view_kb=10, accessed_at=float(i))

        # Total: 5 * 20 KB = 100 KB. Target: 30 KB → evict ~70 KB (at least 3 records).
        freed = db.evict_lru_cache(target_bytes=30 * 1024)
        assert freed >= 60 * 1024

        remaining = db.get_total_cache_size()
        assert remaining <= 40 * 1024


class TestCacheSizeManager:
    def test_disabled_when_zero(self, tmp_path):
        db = MetadataDatabase(str(tmp_path / "test.db"))
        mgr = CacheSizeManager(db, max_cache_size_mb=0)
        assert not mgr.is_cache_full()

    def test_is_cache_full(self, cache_env):
        db, _, _, add = cache_env
        # Add 100 KB of cache.
        add("a.jpg", thumb_kb=25, view_kb=25)
        add("b.jpg", thumb_kb=25, view_kb=25)

        # Limit: 50 KB — cache is over limit.
        mgr = CacheSizeManager(db, max_cache_size_mb=0)
        assert not mgr.is_cache_full()  # disabled

    def test_reports_full_when_over_limit(self, cache_env):
        db, _, _, add = cache_env
        add("a.jpg", thumb_kb=50, view_kb=50)  # 100 KB total

        # Set limit way below (1 KB).  CacheSizeManager takes MB, but we
        # need sub-MB precision for tests — so we construct and patch.
        mgr = CacheSizeManager.__new__(CacheSizeManager)
        mgr._db = db
        mgr._max_bytes = 50 * 1024  # 50 KB limit
        mgr._current_bytes = 0
        mgr._enabled = True
        mgr._lock = __import__("threading").Lock()
        mgr.refresh()

        assert mgr.is_cache_full()

    def test_record_cache_write_triggers_eviction(self, cache_env):
        db, thumb_dir, img_dir, add = cache_env
        add("old.jpg", thumb_kb=10, view_kb=10, accessed_at=1.0)
        add("mid.jpg", thumb_kb=10, view_kb=10, accessed_at=2.0)
        add("new.jpg", thumb_kb=10, view_kb=10, accessed_at=3.0)

        # Total on disk: 60 KB. Set limit to 50 KB.
        mgr = CacheSizeManager.__new__(CacheSizeManager)
        mgr._db = db
        mgr._max_bytes = 50 * 1024  # 50 KB
        mgr._current_bytes = 0
        mgr._enabled = True
        mgr._lock = __import__("threading").Lock()
        mgr.refresh()  # picks up 60 KB

        assert mgr.current_bytes == 60 * 1024

        # Already over limit, record_cache_write should trigger eviction.
        mgr.record_cache_write(0)

        # After eviction to 90% of 50 KB = 45 KB, at least one record removed.
        assert mgr.current_bytes < 50 * 1024


class TestAccessedAtTracking:
    def test_get_thumbnail_paths_touches_accessed_at(self, cache_env):
        db, _, _, add = cache_env
        src = add("a.jpg", thumb_kb=10, view_kb=10, accessed_at=0.0)

        before = time.time()
        db.get_thumbnail_paths(src)
        after = time.time()

        with db._lock:
            cursor = db.conn.cursor()
            cursor.execute("SELECT accessed_at FROM image_metadata WHERE file_path = ?", (src,))
            accessed_at = cursor.fetchone()[0]

        assert before <= accessed_at <= after

    def test_get_cached_thumbnail_paths_touches_accessed_at(self, cache_env):
        db, _, _, add = cache_env
        src = add("a.jpg", thumb_kb=10, view_kb=10, accessed_at=0.0)

        before = time.time()
        db.get_cached_thumbnail_paths(src)
        after = time.time()

        with db._lock:
            cursor = db.conn.cursor()
            cursor.execute("SELECT accessed_at FROM image_metadata WHERE file_path = ?", (src,))
            accessed_at = cursor.fetchone()[0]

        assert before <= accessed_at <= after


class TestRenderManagerGating:
    """Test that _cooperative_generator_runner respects cache_size_manager."""

    def test_background_job_deferred_when_cache_full(self):
        """Low-priority job slices are skipped when cache is full."""
        from core.priority import SourceJob, Priority

        class FakeCacheSizeManager:
            def is_cache_full(self):
                return True

        from core.rendermanager import RenderManager
        rm = RenderManager(num_workers=0)
        rm.cache_size_manager = FakeCacheSizeManager()

        called = []
        def gen():
            called.append("gen")
            yield ["file1"]

        job = SourceJob(
            job_id="post_scan::sess::dir",
            priority=Priority.LOW,
            generator=gen(),
            task_factory=lambda fp, p: [],
            create_tasks=True,
        )
        rm._cooperative_generator_runner(job, 0)
        # Generator should NOT have been called because cache is full.
        assert called == []

    def test_high_priority_job_not_gated(self):
        """GUI-priority jobs bypass the cache-full gate."""
        from core.priority import SourceJob, Priority

        class FakeCacheSizeManager:
            def is_cache_full(self):
                return True

        from core.rendermanager import RenderManager
        rm = RenderManager(num_workers=0)
        rm.cache_size_manager = FakeCacheSizeManager()

        gen_items = []
        def gen():
            gen_items.append("called")
            yield ["file1"]

        job = SourceJob(
            job_id="gui_scan::sess::dir",
            priority=Priority(80),
            generator=gen(),
            task_factory=lambda fp, p: [],
            create_tasks=False,  # reconcile scan doesn't create tasks
        )
        # Patch submit_task to avoid queue operations without workers.
        rm.submit_task = lambda *a, **kw: True
        rm._cooperative_generator_runner(job, 0)
        # Generator SHOULD have been called (high priority, create_tasks=False).
        assert gen_items == ["called"]
