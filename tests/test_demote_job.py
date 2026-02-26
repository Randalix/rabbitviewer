"""Tests for demote-on-disconnect: RenderManager.demote_job and orphan handling."""

import os
import sys
import threading
import time
import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from core.rendermanager import RenderManager
from core.priority import Priority, SourceJob


def _poll(predicate, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=2)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


# ---------------------------------------------------------------------------
# RenderManager.demote_job
# ---------------------------------------------------------------------------

class TestDemoteJob:
    def test_demote_mutates_priority(self, rm: RenderManager):
        """demote_job sets the job's priority to the new value."""
        items = []

        def gen():
            for i in range(20):
                items.append(i)
                yield f"/img{i}.jpg"

        job = SourceJob(
            job_id="gui_scan::sess1::/photos",
            priority=Priority(80),
            generator=gen(),
            task_factory=lambda path, pri: [],
        )
        rm.submit_source_job(job)

        # Demote before the generator finishes all items.
        rm.demote_job("gui_scan::sess1::/photos", Priority.ORPHAN_SCAN)

        with rm.active_jobs_lock:
            demoted = rm.active_jobs.get("gui_scan::sess1::/photos")
        assert demoted is not None
        assert demoted.priority == Priority.ORPHAN_SCAN

        # Let it finish.
        _poll(lambda: "gui_scan::sess1::/photos" not in rm.get_all_job_ids())

    def test_demote_nonexistent_is_noop(self, rm: RenderManager):
        """demote_job on a missing job_id does not raise."""
        rm.demote_job("nonexistent::job", Priority.ORPHAN_SCAN)

    def test_demote_does_not_cancel(self, rm: RenderManager):
        """demote_job leaves the job active (not cancelled)."""
        def gen():
            for i in range(20):
                yield f"/img{i}.jpg"

        job = SourceJob(
            job_id="gui_scan::sess2::/pics",
            priority=Priority(80),
            generator=gen(),
            task_factory=lambda path, pri: [],
        )
        rm.submit_source_job(job)
        rm.demote_job("gui_scan::sess2::/pics", Priority.ORPHAN_SCAN)

        with rm.active_jobs_lock:
            j = rm.active_jobs.get("gui_scan::sess2::/pics")
        assert j is not None
        assert not j.is_cancelled()

        _poll(lambda: "gui_scan::sess2::/pics" not in rm.get_all_job_ids())

    def test_demoted_job_completes(self, rm: RenderManager):
        """A demoted job still runs to completion at the lower priority."""
        results = []

        def gen():
            yield "/a.jpg"
            yield "/b.jpg"

        def factory(path, pri):
            results.append(path)
            return []

        job = SourceJob(
            job_id="gui_scan::sess3::/dir",
            priority=Priority(80),
            generator=gen(),
            task_factory=factory,
            create_tasks=True,
        )
        rm.submit_source_job(job)
        rm.demote_job("gui_scan::sess3::/dir", Priority.ORPHAN_SCAN)

        assert _poll(lambda: "gui_scan::sess3::/dir" not in rm.get_all_job_ids()), \
            "Demoted job should still complete"
        assert sorted(results) == ["/a.jpg", "/b.jpg"]

    def test_demote_vs_cancel_job_stays_active(self, rm: RenderManager):
        """cancel_job removes from active_jobs; demote_job keeps it."""
        def gen():
            for i in range(50):
                yield f"/img{i}.jpg"

        job = SourceJob(
            job_id="gui_scan::sess4::/dir",
            priority=Priority(80),
            generator=gen(),
            task_factory=lambda p, pr: [],
        )
        rm.submit_source_job(job)
        rm.demote_job("gui_scan::sess4::/dir", Priority.ORPHAN_SCAN)

        assert "gui_scan::sess4::/dir" in rm.get_all_job_ids()

        _poll(lambda: "gui_scan::sess4::/dir" not in rm.get_all_job_ids())


# ---------------------------------------------------------------------------
# Job ID prefix matching for disconnect handler
# ---------------------------------------------------------------------------

class TestDisconnectPrefixMatching:
    def test_gui_scan_and_post_scan_matched(self):
        """Both gui_scan and post_scan jobs are matched for demotion."""
        session_id = "abc12345-session-uuid"
        all_jobs = [
            f"gui_scan::{session_id}::/photos",
            f"post_scan::{session_id}::/photos",
            "daemon_idx::/home/user/Photos",
            "watchdog::/photos/new.jpg",
        ]

        _GUI_JOB_PREFIXES = ("gui_scan", "post_scan")
        to_demote = [
            jid for jid in all_jobs
            if jid.startswith(_GUI_JOB_PREFIXES) and session_id in jid
        ]
        assert set(to_demote) == {
            f"gui_scan::{session_id}::/photos",
            f"post_scan::{session_id}::/photos",
        }

    def test_daemon_and_watchdog_not_matched(self):
        """daemon_idx and watchdog jobs survive disconnect."""
        session_id = "sess42"
        all_jobs = [
            "daemon_idx::/photos",
            "watchdog::/photos/x.jpg",
            f"gui_scan::{session_id}::/photos",
        ]

        _GUI_JOB_PREFIXES = ("gui_scan", "post_scan")
        to_demote = [
            jid for jid in all_jobs
            if jid.startswith(_GUI_JOB_PREFIXES) and session_id in jid
        ]
        assert "daemon_idx::/photos" not in to_demote
        assert "watchdog::/photos/x.jpg" not in to_demote

    def test_wrong_session_not_matched(self):
        """Jobs from a different session are not demoted."""
        all_jobs = [
            "gui_scan::sessA::/photos",
            "post_scan::sessA::/photos",
            "gui_scan::sessB::/other",
        ]
        _GUI_JOB_PREFIXES = ("gui_scan", "post_scan")
        to_demote = [
            jid for jid in all_jobs
            if jid.startswith(_GUI_JOB_PREFIXES) and "sessA" in jid
        ]
        assert "gui_scan::sessB::/other" not in to_demote
        assert len(to_demote) == 2


# ---------------------------------------------------------------------------
# ORPHAN_SCAN priority ordering
# ---------------------------------------------------------------------------

class TestOrphanScanPriority:
    def test_orphan_scan_between_background_and_content_hash(self):
        """ORPHAN_SCAN(15) sits between BACKGROUND_SCAN(10) and CONTENT_HASH(20)."""
        assert Priority.BACKGROUND_SCAN < Priority.ORPHAN_SCAN < Priority.CONTENT_HASH
        assert Priority.ORPHAN_SCAN == 15

    def test_orphan_scan_below_low(self):
        """ORPHAN_SCAN is below LOW so it doesn't compete with active GUI work."""
        assert Priority.ORPHAN_SCAN < Priority.LOW
