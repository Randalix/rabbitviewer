"""Tests for the scan_ledger table and orphan recovery flow."""
import os
import sys
import time
import threading

import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from core.metadata_database import MetadataDatabase
from core.rendermanager import RenderManager, Priority, SourceJob
from core.background_indexer import BackgroundIndexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return result


@pytest.fixture()
def db(tmp_path):
    return MetadataDatabase(str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# scan_ledger table basics
# ---------------------------------------------------------------------------

class TestLedgerTable:
    def test_table_exists(self, db):
        cursor = db.ledgers.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_ledger'"
        )
        assert cursor.fetchone() is not None

    def test_batch_insert(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg", "/b.jpg"], scan_root="/photos")
        rows = db.ledgers.conn.execute("SELECT file_path, status FROM scan_ledger").fetchall()
        assert len(rows) == 2
        assert all(status == "discovered" for _, status in rows)

    def test_batch_insert_idempotent(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg"], scan_root="/photos")
        db.ledgers.ledger_batch_insert(["/a.jpg"], scan_root="/photos")
        rows = db.ledgers.conn.execute("SELECT file_path FROM scan_ledger").fetchall()
        assert len(rows) == 1

    def test_mark_complete(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg"], scan_root="/photos")
        db.ledgers.ledger_mark_complete("/a.jpg")
        status = db.ledgers.conn.execute(
            "SELECT status FROM scan_ledger WHERE file_path = '/a.jpg'"
        ).fetchone()[0]
        assert status == "complete"

    def test_mark_complete_missing_path_no_error(self, db):
        db.ledgers.ledger_mark_complete("/nonexistent.jpg")  # should not raise

    def test_get_incomplete(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg", "/b.jpg", "/c.jpg"], scan_root="/photos")
        db.ledgers.ledger_mark_complete("/b.jpg")
        incomplete = db.ledgers.ledger_get_incomplete("/photos")
        assert sorted(incomplete) == ["/a.jpg", "/c.jpg"]

    def test_get_incomplete_different_roots(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg"], scan_root="/photos")
        db.ledgers.ledger_batch_insert(["/x.jpg"], scan_root="/other")
        assert sorted(db.ledgers.ledger_get_incomplete("/photos")) == ["/a.jpg"]
        assert sorted(db.ledgers.ledger_get_incomplete("/other")) == ["/x.jpg"]

    def test_get_walked_dirs(self, db):
        db.ledgers.ledger_batch_insert(
            ["/photos/2024/a.jpg", "/photos/2024/b.jpg", "/photos/2025/c.jpg"],
            scan_root="/photos",
        )
        # Only complete entries count as walked.
        db.ledgers.ledger_mark_complete("/photos/2024/a.jpg")
        db.ledgers.ledger_mark_complete("/photos/2024/b.jpg")
        db.ledgers.ledger_mark_complete("/photos/2025/c.jpg")
        dirs = db.ledgers.ledger_get_walked_dirs("/photos")
        assert dirs == {"/photos/2024", "/photos/2025"}

    def test_get_walked_dirs_excludes_discovered(self, db):
        db.ledgers.ledger_batch_insert(
            ["/photos/done/a.jpg", "/photos/partial/b.jpg"],
            scan_root="/photos",
        )
        db.ledgers.ledger_mark_complete("/photos/done/a.jpg")
        # /photos/partial/b.jpg still 'discovered' — dir should NOT be skipped
        dirs = db.ledgers.ledger_get_walked_dirs("/photos")
        assert dirs == {"/photos/done"}

    def test_prune_complete(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg", "/b.jpg"], scan_root="/photos")
        db.ledgers.ledger_mark_complete("/a.jpg")
        pruned = db.ledgers.ledger_prune_complete("/photos")
        assert pruned == 1
        remaining = db.ledgers.conn.execute("SELECT file_path FROM scan_ledger").fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] == "/b.jpg"

    def test_get_all_scan_roots(self, db):
        db.ledgers.ledger_batch_insert(["/a.jpg"], scan_root="/photos")
        db.ledgers.ledger_batch_insert(["/x.jpg"], scan_root="/nas/archive")
        roots = sorted(db.ledgers.ledger_get_all_scan_roots())
        assert roots == ["/nas/archive", "/photos"]

    def test_empty_ledger(self, db):
        assert db.ledgers.ledger_get_incomplete("/photos") == []
        assert db.ledgers.ledger_get_walked_dirs("/photos") == set()
        assert db.ledgers.ledger_get_all_scan_roots() == []
        assert db.ledgers.ledger_prune_complete("/photos") == 0

    def test_batch_insert_empty_list(self, db):
        db.ledgers.ledger_batch_insert([], scan_root="/photos")  # should not raise
        assert db.ledgers.ledger_get_all_scan_roots() == []


# ---------------------------------------------------------------------------
# on_batch_discovered callback in RenderManager
# ---------------------------------------------------------------------------

class TestOnBatchDiscovered:
    def test_callback_called_with_batch(self):
        rm = RenderManager(num_workers=1)
        rm.start()
        try:
            discovered = []

            def _on_batch(paths):
                discovered.extend(paths)

            def _gen():
                yield ["/a.jpg", "/b.jpg"]

            job = SourceJob(
                job_id="test_discover",
                priority=Priority.LOW,
                generator=_gen(),
                task_factory=lambda path, pri: [],
                on_batch_discovered=_on_batch,
            )
            rm.submit_source_job(job)

            _poll_until(lambda: len(discovered) >= 2)
            assert sorted(discovered) == ["/a.jpg", "/b.jpg"]
        finally:
            rm.shutdown(timeout=5)

    def test_callback_exception_does_not_abort_job(self):
        rm = RenderManager(num_workers=1)
        rm.start()
        try:
            completed = []

            def _on_batch(paths):
                raise RuntimeError("boom")

            def _gen():
                yield ["/a.jpg"]
                yield ["/b.jpg"]

            job = SourceJob(
                job_id="test_exception",
                priority=Priority.LOW,
                generator=_gen(),
                task_factory=lambda path, pri: [],
                on_batch_discovered=_on_batch,
                on_complete=lambda: completed.append(True),
            )
            rm.submit_source_job(job)

            _poll_until(lambda: completed)
            assert completed == [True]
        finally:
            rm.shutdown(timeout=5)


# ---------------------------------------------------------------------------
# skip_dirs in DirectoryScanner
# ---------------------------------------------------------------------------

class TestSkipDirs:
    def test_skip_dirs_skips_walked_directories(self, tmp_path):
        # Create dirs: walked/ and new/
        walked = tmp_path / "walked"
        walked.mkdir()
        (walked / "old.txt").write_text("x")
        new = tmp_path / "new"
        new.mkdir()
        (new / "fresh.txt").write_text("y")

        from core.directory_scanner import DirectoryScanner

        class _FakeTM:
            def get_supported_formats(self):
                return {".txt"}

        scanner = DirectoryScanner(_FakeTM())
        scanner.min_file_size = 0  # accept tiny files

        all_batches = list(scanner.scan_incremental(
            str(tmp_path), recursive=True, skip_dirs={str(walked)},
        ))
        all_files = [f for batch in all_batches for f in batch]
        assert any("fresh.txt" in f for f in all_files)
        assert not any("old.txt" in f for f in all_files)


# ---------------------------------------------------------------------------
# BackgroundIndexer.recover_orphans
# ---------------------------------------------------------------------------

class TestOrphanRecovery:
    def test_recover_orphans_submits_jobs(self, db, tmp_path):
        db.ledgers.ledger_batch_insert(["/nas/a.jpg", "/nas/b.jpg"], scan_root="/nas")

        class _MockRM:
            def __init__(self):
                self.jobs = {}
                self.active_jobs = self.jobs
                self.active_jobs_lock = threading.Lock()

            def submit_source_job(self, job):
                self.jobs[job.job_id] = job

        mock_rm = _MockRM()

        class _FakeTM:
            render_manager = mock_rm
            metadata_db = db

            def create_all_tasks_for_file(self, path, priority):
                return []

        indexer = BackgroundIndexer(_FakeTM(), None, [], metadata_db=db)
        indexer.recover_orphans()

        assert "orphan_recovery::/nas" in mock_rm.jobs
        job = mock_rm.jobs["orphan_recovery::/nas"]
        assert job.priority == Priority.ORPHAN_SCAN

    def test_recover_orphans_no_op_when_empty(self, db):
        class _MockRM:
            def __init__(self):
                self.jobs = {}
                self.active_jobs = self.jobs
                self.active_jobs_lock = threading.Lock()

            def submit_source_job(self, job):
                self.jobs[job.job_id] = job

        mock_rm = _MockRM()

        class _FakeTM:
            render_manager = mock_rm
            metadata_db = db

            def create_all_tasks_for_file(self, path, priority):
                return []

        indexer = BackgroundIndexer(_FakeTM(), None, [], metadata_db=db)
        indexer.recover_orphans()

        assert mock_rm.jobs == {}
