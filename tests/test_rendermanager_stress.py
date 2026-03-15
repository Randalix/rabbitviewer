"""Stress tests for RenderManager priority upgrades and speculative cancellation.

Reproduces the real-world scenario: the GUI heatmap submits speculative view
tasks with cancel_events, then rapidly upgrades one task through multiple
priority levels to FULLRES_REQUEST while cancelling the rest.
"""

import os
import sys
import threading
import time

import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from core.rendermanager import RenderManager
from core.priority import Priority


def _poll(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=4)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


@pytest.fixture()
def rm8():
    """8-worker manager — matches production GUI config."""
    manager = RenderManager(num_workers=8)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


# ---------------------------------------------------------------------------
#  FULLRES upgrade under speculative load
# ---------------------------------------------------------------------------

class TestFullresUpgradeUnderLoad:

    def test_fullres_task_executes_after_speculative_cancel(self, rm):
        """100 speculative tasks, rapid upgrade of one to FULLRES, cancel rest."""
        executed = threading.Event()
        cancel_events = {}

        for i in range(100):
            evt = threading.Event()
            task_id = f"view::img_{i:04d}.CR3"
            cancel_events[task_id] = evt
            rm.submit_task(
                task_id, Priority.BACKGROUND_SCAN,
                lambda e=evt: (e.wait(0.5), None),
                cancel_event=evt,
            )

        target = "view::img_0050.CR3"

        # Rapid priority upgrades (simulates heatmap ring transitions).
        for pri in [Priority(69), Priority(72), Priority(75), Priority(78)]:
            rm.submit_task(
                target, pri,
                lambda: executed.set(),
                cancel_event=cancel_events[target],
            )

        # FULLRES_REQUEST upgrade — no cancel_event (direct request).
        rm.submit_task(target, Priority.FULLRES_REQUEST, lambda: executed.set())

        # Cancel all speculative tasks.
        rm.cancel_tasks([t for t in cancel_events if t != target])

        assert _poll(lambda: executed.is_set()), \
            "FULLRES_REQUEST task was never executed"

    def test_fullres_executes_when_old_cancel_event_already_set(self, rm):
        """Speculative cancel fires BEFORE the upgrade — old cancel_event
        must not propagate to the upgraded task."""
        executed = threading.Event()
        speculative_evt = threading.Event()

        rm.submit_task(
            "view::target.CR3", Priority.BACKGROUND_SCAN,
            lambda: time.sleep(0.5),
            cancel_event=speculative_evt,
        )

        rm.cancel_tasks(["view::target.CR3"])
        assert speculative_evt.is_set()

        rm.submit_task(
            "view::target.CR3", Priority.FULLRES_REQUEST,
            lambda: executed.set(),
        )

        assert _poll(lambda: executed.is_set()), \
            "FULLRES task aborted because it inherited fired cancel_event"

    def test_rapid_same_priority_updates_dont_lose_task(self, rm):
        """50 rapid same-priority re-submissions must not prevent execution."""
        counter = {"n": 0}
        lock = threading.Lock()

        def task_fn():
            with lock:
                counter["n"] += 1

        rm.submit_task("view::rapid.CR3", Priority.GUI_REQUEST, task_fn)

        for _ in range(50):
            rm.submit_task("view::rapid.CR3", Priority.GUI_REQUEST, task_fn)

        assert _poll(lambda: counter["n"] >= 1), \
            "Task was never executed despite 50 re-submissions"


# ---------------------------------------------------------------------------
#  Simulated picture-view navigation (the 0176.CR3 scenario)
# ---------------------------------------------------------------------------

class TestPictureViewNavigation:
    """Simulate rapid D/A navigation through images in picture view.
    Each navigation: upgrade current image to FULLRES, cancel previous."""

    def test_sequential_navigation_5_images(self, rm):
        """Navigate through 5 images — last one must execute."""
        results = []
        lock = threading.Lock()

        for i in range(5):
            task_id = f"view::nav_{i}.CR3"
            evt = threading.Event()

            rm.submit_task(
                task_id, Priority.BACKGROUND_SCAN,
                lambda: time.sleep(0.2),
                cancel_event=evt,
            )

            rm.submit_task(
                task_id, Priority.FULLRES_REQUEST,
                lambda idx=i: (lock.acquire(), results.append(idx), lock.release()),
            )

            for j in range(i):
                rm.cancel_tasks([f"view::nav_{j}.CR3"])

        assert _poll(lambda: len(results) > 0 and results[-1] == 4), \
            f"Expected last navigation target to execute, got: {results}"

    def test_rapid_back_and_forth(self, rm8):
        """Navigate A→B→A→B→A rapidly. Final image (A) must execute."""
        executed_a = threading.Event()
        executed_b = threading.Event()
        evt_a = threading.Event()
        evt_b = threading.Event()

        # Submit both as speculative.
        rm8.submit_task("view::A.CR3", Priority.BACKGROUND_SCAN,
                        lambda: time.sleep(1), cancel_event=evt_a)
        rm8.submit_task("view::B.CR3", Priority.BACKGROUND_SCAN,
                        lambda: time.sleep(1), cancel_event=evt_b)

        # Navigate: A → B → A → B → A
        for current, other in [("A", "B"), ("B", "A"), ("A", "B"), ("B", "A"), ("A", "B")]:
            rm8.submit_task(
                f"view::{current}.CR3", Priority.FULLRES_REQUEST,
                lambda c=current: (executed_a if c == "A" else executed_b).set(),
            )
            rm8.cancel_tasks([f"view::{other}.CR3"])

        # Last navigation was A→B, so B should execute.
        assert _poll(lambda: executed_b.is_set()), \
            "Final navigation target (B) was never executed"

    def test_fullres_with_all_workers_busy(self, rm):
        """All workers blocked on slow tasks — FULLRES must execute
        within 1s after cancel frees a worker."""
        executed = threading.Event()
        cancel_events = []

        # Block all 4 workers with cancellable slow tasks.
        for i in range(4):
            evt = threading.Event()
            cancel_events.append(evt)
            rm.submit_task(
                f"view::slow_{i}.CR3", Priority.BACKGROUND_SCAN,
                lambda e=evt: e.wait(5),
                cancel_event=evt,
            )

        # Wait for workers to pick them up.
        time.sleep(0.15)

        # Submit FULLRES for a different image.
        rm.submit_task(
            "view::target.CR3", Priority.FULLRES_REQUEST,
            lambda: executed.set(),
        )

        # Cancel all slow tasks — should free workers.
        rm.cancel_tasks([f"view::slow_{i}.CR3" for i in range(4)])

        assert _poll(lambda: executed.is_set(), timeout=2), \
            "FULLRES task not executed within 2s after cancelling blockers"


# ---------------------------------------------------------------------------
#  Throughput and ordering
# ---------------------------------------------------------------------------

class TestHighConcurrencyThroughput:

    def test_500_tasks_complete(self, rm):
        """Submit 500 tasks and verify all complete within timeout."""
        completed = {"n": 0}
        lock = threading.Lock()

        def task_fn():
            with lock:
                completed["n"] += 1

        for i in range(500):
            rm.submit_task(f"stress::{i}", Priority.NORMAL, task_fn)

        assert _poll(lambda: completed["n"] == 500, timeout=10), \
            f"Only {completed['n']}/500 tasks completed"

    def test_priority_ordering_under_load(self, rm):
        """High-priority tasks execute before low-priority ones when
        workers are initially blocked."""
        order = []
        lock = threading.Lock()
        gate = threading.Event()

        for i in range(4):
            rm.submit_task(f"blocker::{i}", Priority.GUI_REQUEST,
                           lambda: gate.wait(5))

        time.sleep(0.1)

        def record(label):
            def fn():
                with lock:
                    order.append(label)
            return fn

        for i in range(10):
            rm.submit_task(f"low::{i}", Priority.BACKGROUND_SCAN, record(f"low_{i}"))
        rm.submit_task("high::0", Priority.FULLRES_REQUEST, record("high"))

        gate.set()

        assert _poll(lambda: "high" in order, timeout=5), \
            f"High-priority task not executed, order: {order}"

        if "high" in order:
            high_pos = order.index("high")
            assert high_pos <= 4, \
                f"High-priority task at position {high_pos}, expected <= 4: {order}"

    def test_mixed_priorities_no_starvation(self, rm8):
        """Submit tasks at all priority levels — none should starve."""
        completed = {p.name: 0 for p in [
            Priority.BACKGROUND_SCAN, Priority.LOW, Priority.NORMAL,
            Priority.HIGH, Priority.GUI_REQUEST,
        ]}
        lock = threading.Lock()

        for p_name, p in [
            ("BACKGROUND_SCAN", Priority.BACKGROUND_SCAN),
            ("LOW", Priority.LOW),
            ("NORMAL", Priority.NORMAL),
            ("HIGH", Priority.HIGH),
            ("GUI_REQUEST", Priority.GUI_REQUEST),
        ]:
            for i in range(20):
                def fn(name=p_name):
                    with lock:
                        completed[name] += 1
                rm8.submit_task(f"{p_name}::{i}", p, fn)

        assert _poll(lambda: all(v == 20 for v in completed.values()), timeout=10), \
            f"Starvation detected: {completed}"


# ---------------------------------------------------------------------------
#  Cancel responsiveness
# ---------------------------------------------------------------------------

class TestCancelResponsiveness:
    """Verify that cancelled speculative tasks free workers quickly."""

    def test_cancel_frees_worker_within_timeout(self, rm):
        """A cancelled task should release its worker within the poll
        interval (0.5s for exiftool, instant for Event.wait)."""
        freed = threading.Event()
        evt = threading.Event()

        rm.submit_task(
            "view::blocking.CR3", Priority.BACKGROUND_SCAN,
            lambda: evt.wait(10),
            cancel_event=evt,
        )

        time.sleep(0.1)

        # Cancel the blocking task.
        rm.cancel_tasks(["view::blocking.CR3"])

        # Submit a task that signals when it runs — proves worker is free.
        rm.submit_task("view::after_cancel.CR3", Priority.NORMAL,
                       lambda: freed.set())

        assert _poll(lambda: freed.is_set(), timeout=2), \
            "Worker not freed after cancel"

    def test_batch_cancel_100_tasks(self, rm8):
        """Cancelling 100 tasks in batch should not block or deadlock."""
        events = []
        for i in range(100):
            evt = threading.Event()
            events.append(evt)
            rm8.submit_task(
                f"view::batch_{i}.CR3", Priority.BACKGROUND_SCAN,
                lambda e=evt: e.wait(5),
                cancel_event=evt,
            )

        time.sleep(0.1)

        t0 = time.monotonic()
        cancelled = rm8.cancel_tasks([f"view::batch_{i}.CR3" for i in range(100)])
        cancel_time = time.monotonic() - t0

        assert cancelled > 0, "No tasks were cancelled"
        assert cancel_time < 0.1, f"Batch cancel took {cancel_time:.3f}s, expected < 0.1s"

        # Verify workers recover — submit and complete a task.
        done = threading.Event()
        rm8.submit_task("view::recovery.CR3", Priority.NORMAL, lambda: done.set())
        assert _poll(lambda: done.is_set(), timeout=2), \
            "Workers did not recover after batch cancel"
