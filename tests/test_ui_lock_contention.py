"""Verify that read operations on ImageTable do not block on the write lock.

Thread-local read connections (WAL) allow concurrent reads while writes
hold ``_lock``.
"""

import sqlite3
import threading
import time

import pytest

from core.db.image_table import ImageTable


def _create_schema(db_path: str) -> None:
    """Create the image_metadata table using the real schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            thumbnail_path TEXT,
            view_image_path TEXT,
            content_hash TEXT,
            metadata_hash TEXT,
            file_size INTEGER,
            width INTEGER,
            height INTEGER,
            format TEXT,
            color_space TEXT,
            has_alpha INTEGER DEFAULT 0,
            rating INTEGER DEFAULT 0,
            orientation INTEGER DEFAULT 1,
            exif_data TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            accessed_at REAL,
            birthtime REAL,
            mtime REAL,
            sidecars TEXT DEFAULT '[]',
            camera_make TEXT,
            camera_model TEXT,
            date_taken TEXT
        )
    """)
    # Seed some rows directly so we don't depend on store_metadata internals.
    t = time.time()
    for i in range(50):
        conn.execute(
            "INSERT INTO image_metadata (file_path, orientation, created_at, updated_at) "
            "VALUES (?, 1, ?, ?)",
            (f"/fake/img_{i:04d}.jpg", t, t),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def table(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_schema(db_path)
    return ImageTable(db_path)


class TestReadWriteConcurrency:
    """Confirm that readers are not blocked by the write lock."""

    def test_reader_not_blocked_by_writer(self, table):
        """A writer holds _lock for 200 ms. The reader must complete without waiting."""
        paths = [f"/fake/img_{i:04d}.jpg" for i in range(5)]
        writer_hold_time = 0.2

        barrier = threading.Barrier(2, timeout=5)
        reader_wait_ms = []

        def slow_writer():
            with table._lock:
                barrier.wait()
                time.sleep(writer_hold_time)

        def reader():
            barrier.wait()
            t0 = time.perf_counter()
            table.get_metadata_batch(paths)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            reader_wait_ms.append(elapsed_ms)

        writer_thread = threading.Thread(target=slow_writer)
        reader_thread = threading.Thread(target=reader)

        writer_thread.start()
        reader_thread.start()
        writer_thread.join(timeout=5)
        reader_thread.join(timeout=5)

        assert reader_wait_ms, "Reader did not run"
        wait = reader_wait_ms[0]
        # Reader should complete in <20 ms even while writer holds _lock for 200 ms.
        assert wait < 20, (
            f"Reader was blocked for {wait:.1f} ms — thread-local read "
            f"connections should bypass the write lock"
        )

    def test_batch_reads_under_contention(self, table):
        """N sequential reads complete fast even with a writer holding _lock."""
        paths = [f"/fake/img_{i:04d}.jpg" for i in range(50)]
        num_reads = 10

        stop = threading.Event()

        def writer_loop():
            while not stop.is_set():
                with table._lock:
                    time.sleep(0.05)

        writer = threading.Thread(target=writer_loop, daemon=True)
        writer.start()
        time.sleep(0.1)  # let writer start contending

        t0 = time.perf_counter()
        for i in range(num_reads):
            table.get_metadata_batch([paths[i]])
        total_ms = (time.perf_counter() - t0) * 1000

        stop.set()
        writer.join(timeout=2)

        # 10 reads should take <50 ms total even under write contention.
        assert total_ms < 50, (
            f"{num_reads} reads took {total_ms:.0f} ms — expected <50 ms "
            f"with thread-local read connections"
        )
