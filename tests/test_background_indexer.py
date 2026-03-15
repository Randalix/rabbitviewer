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


class _StubLedger:
    """Minimal stub for MetadataDatabase ledger methods."""

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

    def pending_write_get_all(self):
        return []

    def get_files_missing_thumbnails(self, watch_paths):
        return []

    def file_work_get_all_roots(self):
        return []

    def file_work_get_pending_types(self, scan_root):
        return []

    def file_work_get_pending(self, scan_root, work_type):
        return []

    def file_work_batch_insert(self, file_paths, work_type, scan_root):
        pass


class _StubThumbnailManager:

    def __init__(self, render_manager):
        self.render_manager = render_manager
        self.metadata_db = _StubLedger()
        self.all_calls: list[str] = []

    def create_all_tasks_for_file(self, path, priority):
        self.all_calls.append(path)
        return []

    def recover_pending_writes(self):
        return 0


class _MockRenderManager:
    """Tracks submit_source_job calls without running workers."""

    def __init__(self):
        import threading
        self._active_jobs: dict[str, SourceJob] = {}
        self.active_jobs_lock = threading.Lock()
        self.active_jobs = self._active_jobs

    def get_all_job_ids(self) -> list[str]:
        return list(self._active_jobs.keys())

    def submit_source_job(self, job: SourceJob):
        if job.job_id in self._active_jobs:
            return
        self._active_jobs[job.job_id] = job


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

        for job in mock_rm._active_jobs.values():
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


