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


class TestFullresUpgradeUnderLoad:
    """Simulate: 100 speculative view tasks, rapid priority upgrade of one
    task to FULLRES_REQUEST, cancel all other speculative tasks.  The
    FULLRES task must execute."""

    def test_fullres_task_executes_after_speculative_cancel(self, rm):
        executed = threading.Event()
        cancel_events = {}

        # Submit 100 speculative view tasks (slow — 0.5s each).
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
        rm.submit_task(
            target, Priority.FULLRES_REQUEST,
            lambda: executed.set(),
        )

        # Cancel all speculative tasks (like request_view_image does).
        speculative = [
            tid for tid in cancel_events
            if tid != target
        ]
        rm.cancel_tasks(speculative)

        assert _poll(lambda: executed.is_set()), \
            "FULLRES_REQUEST task was never executed"

    def test_fullres_executes_when_old_cancel_event_already_set(self, rm):
        """The specific bug: speculative cancel fires BEFORE the upgrade.
        The old cancel_event is already set when FULLRES_REQUEST arrives."""
        executed = threading.Event()
        speculative_evt = threading.Event()

        # Submit speculative task.
        rm.submit_task(
            "view::target.CR3", Priority.BACKGROUND_SCAN,
            lambda: time.sleep(0.5),
            cancel_event=speculative_evt,
        )

        # Cancel ALL speculative tasks (including target) — sets the event.
        rm.cancel_tasks(["view::target.CR3"])
        assert speculative_evt.is_set()

        # Now upgrade to FULLRES_REQUEST — must NOT inherit the fired event.
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

        # Rapid same-priority updates (simulates heatmap refreshes).
        for _ in range(50):
            rm.submit_task("view::rapid.CR3", Priority.GUI_REQUEST, task_fn)

        assert _poll(lambda: counter["n"] >= 1), \
            "Task was never executed despite 50 re-submissions"

    def test_interleaved_upgrades_and_cancels(self, rm):
        """Multiple tasks upgraded to FULLRES_REQUEST in sequence (user
        navigating through images rapidly)."""
        results = []
        lock = threading.Lock()

        for i in range(5):
            task_id = f"view::nav_{i}.CR3"
            evt = threading.Event()

            # Submit as speculative.
            rm.submit_task(
                task_id, Priority.BACKGROUND_SCAN,
                lambda: time.sleep(0.2),
                cancel_event=evt,
            )

            # Immediately upgrade to FULLRES and cancel all others.
            rm.submit_task(
                task_id, Priority.FULLRES_REQUEST,
                lambda idx=i: (lock.acquire(), results.append(idx), lock.release()),
            )

            # Cancel previous FULLRES tasks (simulate user moving on).
            for j in range(i):
                rm.cancel_tasks([f"view::nav_{j}.CR3"])

        # At least the last task should execute.
        assert _poll(lambda: len(results) > 0 and results[-1] == 4), \
            f"Expected last navigation target to execute, got: {results}"


class TestHighConcurrencyThroughput:
    """Measure that the RenderManager doesn't deadlock or starve under
    high task submission rates."""

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

        # Block all workers.
        for i in range(4):
            rm.submit_task(f"blocker::{i}", Priority.GUI_REQUEST,
                           lambda: gate.wait(5))

        # Wait for workers to pick up blockers.
        time.sleep(0.1)

        # Submit low then high priority.
        def record(label):
            def fn():
                with lock:
                    order.append(label)
            return fn

        for i in range(10):
            rm.submit_task(f"low::{i}", Priority.BACKGROUND_SCAN, record(f"low_{i}"))
        rm.submit_task("high::0", Priority.FULLRES_REQUEST, record("high"))

        # Release workers.
        gate.set()

        assert _poll(lambda: "high" in order, timeout=5), \
            f"High-priority task not executed, order: {order}"

        # High should execute before most lows.
        if "high" in order:
            high_pos = order.index("high")
            assert high_pos <= 4, \
                f"High-priority task at position {high_pos}, expected <= 4: {order}"
