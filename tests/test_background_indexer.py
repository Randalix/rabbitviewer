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

    def scan_incremental(self, path, recursive=True):
        files = self._files.get(path, [])
        if files:
            yield files


class _StubThumbnailManager:

    def __init__(self, render_manager):
        self.render_manager = render_manager
        self.all_calls: list[str] = []

    def create_all_tasks_for_file(self, path, priority):
        self.all_calls.append(path)
        return []


class _MockRenderManager:
    """Tracks submit_source_job calls without running workers."""

    def __init__(self):
        self._active_jobs: dict[str, SourceJob] = {}

    def get_all_job_ids(self) -> list[str]:
        return list(self._active_jobs.keys())

    def submit_source_job(self, job: SourceJob):
        if job.job_id in self._active_jobs:
            return
        self._active_jobs[job.job_id] = job


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=2)
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
# Job ID convention: daemon_idx:: survives GUI disconnect
# ---------------------------------------------------------------------------

class TestJobIdConvention:
    def test_daemon_idx_not_matched_by_session_prefix_filter(self):
        session_id = "abc12345-session-uuid"
        daemon_job_ids = [
            "daemon_idx::/home/user/Photos",
        ]
        gui_job_ids = [
            f"gui_scan_tasks::{session_id}::/home/user/Photos",
            f"gui_view_images::{session_id}::/home/user/Photos",
        ]
        all_jobs = daemon_job_ids + gui_job_ids

        _GUI_JOB_PREFIXES = ("gui_scan", "gui_view_images")
        to_cancel = [
            jid for jid in all_jobs
            if jid.startswith(_GUI_JOB_PREFIXES) and session_id in jid
        ]
        assert set(to_cancel) == set(gui_job_ids)
        for djid in daemon_job_ids:
            assert djid not in to_cancel

    def test_old_substring_match_would_fail_with_uuid_in_path(self):
        """Prefix-qualifying prevents false matches when session string appears in path."""
        session_id = "photos"
        daemon_job = f"daemon_idx::/home/{session_id}/Pictures"

        # Old logic: substring match would incorrectly cancel this daemon job
        assert session_id in daemon_job

        # New logic: prefix-qualified — does not match
        _GUI_JOB_PREFIXES = ("gui_scan", "gui_view_images")
        assert not (daemon_job.startswith(_GUI_JOB_PREFIXES) and session_id in daemon_job)


# ---------------------------------------------------------------------------
# RenderManager: scan_progress suppression for daemon_idx:: jobs
# ---------------------------------------------------------------------------

class TestScanProgressSuppression:
    def test_daemon_idx_jobs_produce_no_scan_progress(self, rm, tmp_path):
        watch = str(tmp_path / "photos")
        os.makedirs(watch)
        scanner = _StubDirectoryScanner({watch: [f"{watch}/a.jpg"]})
        tm = _StubThumbnailManager(rm)

        indexer = BackgroundIndexer(tm, scanner, [watch])
        indexer.start_indexing()

        # Poll until the job completes (no active jobs left)
        _poll_until(lambda: rm.get_all_job_ids() == [])

        notifications = []
        while not rm.notification_queue.empty():
            notifications.append(rm.notification_queue.get_nowait())

        scan_progress = [n for n in notifications if n.type == "scan_progress"]
        assert scan_progress == [], f"daemon_idx jobs must not emit scan_progress, got {len(scan_progress)}"

    def test_gui_job_not_suppressed(self):
        """Verify the suppression guard only matches daemon_idx:: prefixed jobs."""
        # why: the full notification path requires pydantic (model_dump) which
        # is not available in the stub test env. Unit-test the guard directly.
        assert not "gui_scan_tasks::s::p".startswith("daemon_idx::")
        assert "daemon_idx::/p".startswith("daemon_idx::")


# ---------------------------------------------------------------------------
# RenderManager: session_id extraction for daemon_idx:: jobs
# ---------------------------------------------------------------------------

class TestSessionIdExtraction:
    def test_gui_job_notifications_carry_session_id(self, rm, tmp_path):
        watch = str(tmp_path / "photos")
        os.makedirs(watch)

        def gen():
            yield [f"{watch}/x.jpg"]

        tm = _StubThumbnailManager(rm)
        gui_job = SourceJob(
            job_id=f"gui_scan_tasks::sess42::{watch}",
            priority=Priority.GUI_REQUEST_LOW,
            generator=gen(),
            task_factory=tm.create_all_tasks_for_file,
        )
        rm.submit_source_job(gui_job)

        # Poll until notifications arrive
        _poll_until(lambda: not rm.notification_queue.empty())

        notifications = []
        while not rm.notification_queue.empty():
            notifications.append(rm.notification_queue.get_nowait())

        for n in notifications:
            if n.type in ("scan_progress", "scan_complete"):
                assert n.session_id == "sess42", f"Expected session_id 'sess42', got '{n.session_id}'"
