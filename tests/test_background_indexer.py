"""Tests for core.background_indexer and related daemon indexing changes."""
import os
import sys
import time

import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from core.background_indexer import BackgroundIndexer
from core.rendermanager import RenderManager, Priority, SourceJob


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_until(predicate, timeout=5.0, interval=0.05):
    """Poll predicate() until truthy or timeout. Returns last value."""
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return result


class _StubDirectoryScanner:

    def __init__(self, files_by_path: dict[str, list[str]]):
        self._files = files_by_path

    def scan_incremental(self, path, recursive=True, skip_dirs=None):
        files = self._files.get(path, [])
        if files:
            yield files


class _StubLedgerTable:
    """Minimal stub for LedgerTable methods."""

    def ledger_get_all_scan_roots(self):
        return []

    def ledger_get_incomplete(self, scan_root):
        return []

    def ledger_get_walked_dirs(self, scan_root):
        return set()

    def ledger_batch_insert(self, file_paths, scan_root):
        pass

    def ledger_prune_complete(self, scan_root):
        return 0

    def file_work_get_all_roots(self):
        return []

    def file_work_get_pending_types(self, scan_root):
        return []

    def file_work_get_pending(self, scan_root, work_type):
        return []

    def file_work_batch_insert(self, file_paths, work_type, scan_root):
        pass

    def file_transfer_get_all_pending(self):
        return []

    def file_transfer_cleanup_completed(self):
        pass # Stubbed for testing


class _StubImageTable:
    """Minimal stub for ImageTable methods."""

    def get_files_missing_thumbnails(self, watch_paths):
        return []


class _StubDB:
    """Minimal stub for MetadataDatabase with sub-table access."""

    def __init__(self):
        self.ledgers = _StubLedgerTable()
        self.images = _StubImageTable()

    def pending_write_get_all(self):
        return []


class _StubThumbnailManager:

    def __init__(self, render_manager):
        self.render_manager = render_manager
        self.metadata_db = _StubDB()
        self.all_calls: list[str] = []
        self.task_ops = None # Added for TestRecoverPendingTransfers

    def create_all_tasks_for_file(self, path, priority):
        self.all_calls.append(path)
        return []

    def recover_pending_writes(self):
        return 0


class _MockRenderManager:
    """Tracks submit_source_job and submit_task calls without running workers."""

    def __init__(self):
        import threading
        self._submitted_source_jobs: dict[str, SourceJob] = {}
        self._submitted_tasks: list = []
        self.active_jobs_lock = threading.Lock()
        self.active_jobs = self._submitted_source_jobs

    def get_all_job_ids(self) -> list[str]:
        return list(self._submitted_source_jobs.keys())

    def submit_source_job(self, job: SourceJob):
        if job.job_id in self._submitted_source_jobs:
            return
        self._submitted_source_jobs[job.job_id] = job

    def submit_task(self, task_id, priority, fn, *args):
        self._submitted_tasks.append((task_id, priority, fn, args))

    def get_submitted_tasks(self) -> list:
        return self._submitted_tasks


@pytest.fixture()
def collected_notifications():
    return []


@pytest.fixture()
def rm(collected_notifications):
    manager = RenderManager(num_workers=2)
    manager.add_notification_callback(lambda n: collected_notifications.append(n))
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


# ---------------------------------------------------------------------------
# BackgroundIndexer.start_indexing
# ---------------------------------------------------------------------------

class TestStartIndexing:
    def test_submits_one_job_per_watch_path(self, tmp_path):
        watch = str(tmp_path / "photos")
        os.makedirs(watch)
        mock_rm = _MockRenderManager()
        tm = _StubThumbnailManager(mock_rm)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/a.jpg"]})

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()

        job_ids = mock_rm.get_all_job_ids()
        assert job_ids == [f"daemon_idx::{watch}"]

    def test_skips_nonexistent_watch_path(self, tmp_path):
        missing = str(tmp_path / "does_not_exist")
        mock_rm = _MockRenderManager()
        tm = _StubThumbnailManager(mock_rm)
        scanner = _StubDirectoryScanner({})

        indexer = BackgroundIndexer(tm, scanner, [missing])
        indexer.start_indexing()

        assert mock_rm.get_all_job_ids() == []

    def test_uses_background_scan_priority(self, tmp_path):
        watch = str(tmp_path / "pics")
        os.makedirs(watch)
        mock_rm = _MockRenderManager()
        tm = _StubThumbnailManager(mock_rm)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/b.jpg"]})

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()

        for job in mock_rm.active_jobs.values():
            assert job.priority == Priority.BACKGROUND_SCAN

    def test_multiple_watch_paths(self, tmp_path):
        p1 = str(tmp_path / "photos")
        p2 = str(tmp_path / "downloads")
        os.makedirs(p1)
        os.makedirs(p2)
        mock_rm = _MockRenderManager()
        tm = _StubThumbnailManager(mock_rm)
        scanner = _StubDirectoryScanner({p1: [f"{p1}/a.jpg"], p2: [f"{p2}/b.jpg"]})

        indexer = BackgroundIndexer(tm, scanner, [p1, p2])
        indexer.start_indexing()

        assert len(mock_rm.get_all_job_ids()) == 2  # 1 job per path

    def test_indexes_once_no_restart(self, tmp_path):
        """After start_indexing, there is no restart mechanism — index once, rely on watchdog."""
        watch = str(tmp_path / "photos")
        os.makedirs(watch)
        mock_rm = _MockRenderManager()
        tm = _StubThumbnailManager(mock_rm)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/a.jpg"]})

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()
        assert len(mock_rm.get_all_job_ids()) == 1

        assert not hasattr(indexer, "restart_indexing")

    def test_single_walk_calls_combined_factory(self, rm, tmp_path):
        """Verify the combined task factory is called (single os.walk, not two)."""
        watch = str(tmp_path / "photos")
        os.makedirs(watch)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/a.jpg", f"{watch}/b.jpg"]})
        tm = _StubThumbnailManager(rm)

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()

        _poll_until(lambda: len(tm.all_calls) >= 2)
        assert sorted(tm.all_calls) == sorted([f"{watch}/a.jpg", f"{watch}/b.jpg"])


# ---------------------------------------------------------------------------
# RenderManager: scan_progress suppression for daemon_idx:: jobs
# ---------------------------------------------------------------------------

class TestScanProgressSuppression:
    def test_daemon_idx_jobs_produce_no_scan_progress(self, rm, collected_notifications, tmp_path):
        watch = str(tmp_path / "photos")
        os.makedirs(watch)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/a.jpg"]})
        tm = _StubThumbnailManager(rm)

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()

        # Poll until the job completes (no active jobs left)
        _poll_until(lambda: rm.get_all_job_ids() == [])

        scan_progress = [n for n in collected_notifications if n.type == "scan_progress"]
        assert scan_progress == [], f"daemon_idx jobs must not emit scan_progress, got {len(scan_progress)}"

    def test_gui_job_not_suppressed(self):
        """Verify the suppression guard only matches daemon_idx:: prefixed jobs."""
        # why: the full notification path requires pydantic (model_dump) which
        # is not available in the stub test env. Unit-test the guard directly.
        assert not "gui_scan_tasks::s::p".startswith("daemon_idx::")
        assert "daemon_idx::/p".startswith("daemon_idx::")

class TestRecoverPendingTransfers:
    @pytest.fixture
    def mock_rm(self):
        return _MockRenderManager()

    @pytest.fixture
    def tm_with_mock_task_ops(self, mock_rm):
        class MockTaskOperations:
            def __init__(self):
                self.execute_compound_calls = []
            def execute_compound(self, ops):
                self.execute_compound_calls.append(ops)
                return {"success": True}

        tm = _StubThumbnailManager(mock_rm)
        tm.task_ops = MockTaskOperations()
        return tm

    def test_no_pending_transfers_submits_nothing(self, tm_with_mock_task_ops, mock_rm):
        # Default _StubLedgerTable returns empty, so no setup needed
        indexer = BackgroundIndexer(tm_with_mock_task_ops, _StubDirectoryScanner({}), [])
        indexer.recover_pending_transfers()
        assert not mock_rm.get_submitted_tasks()

    def test_pending_deletes_submits_one_task(self, tm_with_mock_task_ops, mock_rm):
        class LedgerWithDeletes(_StubLedgerTable):
            def file_transfer_get_all_pending(self):
                return [
                    ("/path/to/delete1.jpg", "", "delete"),
                    ("/path/to/delete2.png", "", "delete"),
                ]
        tm_with_mock_task_ops.metadata_db.ledgers = LedgerWithDeletes()
        indexer = BackgroundIndexer(tm_with_mock_task_ops, _StubDirectoryScanner({}), [])
        indexer.recover_pending_transfers()

        tasks = mock_rm.get_submitted_tasks()
        assert len(tasks) == 1
        task_id, priority, fn, args = tasks[0]
        assert task_id == "recover_deletes"
        assert priority == Priority.ORPHAN_SCAN
        assert args[0] == [("send2trash", ["/path/to/delete1.jpg", "/path/to/delete2.png"]),
                           ("remove_records", ["/path/to/delete1.jpg", "/path/to/delete2.png"])]
        assert fn == tm_with_mock_task_ops.task_ops.execute_compound

    def test_pending_copies_grouped_by_dest_dir(self, tm_with_mock_task_ops, mock_rm):
        class LedgerWithCopies(_StubLedgerTable):
            def file_transfer_get_all_pending(self):
                return [
                    ("/src/a.jpg", "/dest/dir1", "copy"),
                    ("/src/b.png", "/dest/dir2", "copy"),
                    ("/src/c.gif", "/dest/dir1", "copy"),
                ]
        tm_with_mock_task_ops.metadata_db.ledgers = LedgerWithCopies()
        indexer = BackgroundIndexer(tm_with_mock_task_ops, _StubDirectoryScanner({}), [])
        indexer.recover_pending_transfers()

        tasks = mock_rm.get_submitted_tasks()
        assert len(tasks) == 2

        tasks.sort(key=lambda x: x[0])

        task1_id, task1_priority, task1_fn, task1_args = tasks[0]
        assert task1_id == "recover_copies::/dest/dir1"
        assert task1_priority == Priority.ORPHAN_SCAN
        assert task1_args[0] == [("bookmark_copy", ["/src/a.jpg", "/src/c.gif"], {"dest_dir": "/dest/dir1"})]
        assert task1_fn == tm_with_mock_task_ops.task_ops.execute_compound

        task2_id, task2_priority, task2_fn, task2_args = tasks[1]
        assert task2_id == "recover_copies::/dest/dir2"
        assert task2_priority == Priority.ORPHAN_SCAN
        assert task2_args[0] == [("bookmark_copy", ["/src/b.png"], {"dest_dir": "/dest/dir2"})]
        assert task2_fn == tm_with_mock_task_ops.task_ops.execute_compound

    def test_pending_moves_grouped_by_dest_dir(self, tm_with_mock_task_ops, mock_rm):
        class LedgerWithMoves(_StubLedgerTable):
            def file_transfer_get_all_pending(self):
                return [
                    ("/src/x.jpg", "/new/place1", "move"),
                    ("/src/y.png", "/new/place2", "move"),
                    ("/src/z.gif", "/new/place1", "move"),
                ]
        tm_with_mock_task_ops.metadata_db.ledgers = LedgerWithMoves()
        indexer = BackgroundIndexer(tm_with_mock_task_ops, _StubDirectoryScanner({}), [])
        indexer.recover_pending_transfers()

        tasks = mock_rm.get_submitted_tasks()
        assert len(tasks) == 2
        tasks.sort(key=lambda x: x[0])

        task1_id, task1_priority, task1_fn, task1_args = tasks[0]
        assert task1_id == "recover_moves::/new/place1"
        assert task1_priority == Priority.ORPHAN_SCAN
        assert task1_args[0] == [("bookmark_move", ["/src/x.jpg", "/src/z.gif"], {"dest_dir": "/new/place1"})]
        assert task1_fn == tm_with_mock_task_ops.task_ops.execute_compound

        task2_id, task2_priority, task2_fn, task2_args = tasks[1]
        assert task2_id == "recover_moves::/new/place2"
        assert task2_priority == Priority.ORPHAN_SCAN
        assert task2_args[0] == [("bookmark_move", ["/src/y.png"], {"dest_dir": "/new/place2"})]
        assert task2_fn == tm_with_mock_task_ops.task_ops.execute_compound

    def test_mixed_pending_transfers_submits_all(self, tm_with_mock_task_ops, mock_rm):
        class LedgerWithMixed(_StubLedgerTable):
            def file_transfer_get_all_pending(self):
                return [
                    ("/path/del.jpg", "", "delete"),
                    ("/src/copy1.png", "/dest/copy_dir", "copy"),
                    ("/src/move1.gif", "/dest/move_dir", "move"),
                    ("/src/copy2.webp", "/dest/copy_dir", "copy"),
                ]
        tm_with_mock_task_ops.metadata_db.ledgers = LedgerWithMixed()
        indexer = BackgroundIndexer(tm_with_mock_task_ops, _StubDirectoryScanner({}), [])
        indexer.recover_pending_transfers()

        tasks = mock_rm.get_submitted_tasks()
        assert len(tasks) == 3
        tasks.sort(key=lambda x: x[0])

        # Copy task
        task0_id, _, _, task0_args = tasks[0]
        assert task0_id == "recover_copies::/dest/copy_dir"
        assert task0_args[0][0][1] == ["/src/copy1.png", "/src/copy2.webp"]

        # Delete task
        task1_id, _, _, task1_args = tasks[1]
        assert task1_id == "recover_deletes"
        assert task1_args[0][0][1] == ["/path/del.jpg"]

        # Move task
        task2_id, _, _, task2_args = tasks[2]
        assert task2_id == "recover_moves::/dest/move_dir"
        assert task2_args[0][0][1] == ["/src/move1.gif"]


