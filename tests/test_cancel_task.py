"""Tests for RenderManager.cancel_task cooperative cancellation."""

import os
import sys
import threading
import time
import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from core.rendermanager import RenderManager
from core.priority import Priority


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=2)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


def _poll(predicate, timeout=2.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestCancelTask:
    def test_cancel_sets_event(self, rm: RenderManager):
        """cancel_task returns True and sets the event on an existing task."""
        evt = threading.Event()
        blocker = threading.Event()
        rm.submit_task(
            "test::cancel", Priority.LOW,
            lambda: blocker.wait(2),
            cancel_event=evt,
        )
        assert rm.cancel_task("test::cancel") is True
        assert evt.is_set()
        blocker.set()

    def test_cancel_sets_is_active_false(self, rm: RenderManager):
        """cancel_task marks is_active=False for fast worker discard."""
        evt = threading.Event()
        blocker = threading.Event()
        rm.submit_task(
            "test::active", Priority.LOW,
            lambda: blocker.wait(2),
            cancel_event=evt,
        )
        rm.cancel_task("test::active")
        with rm.graph_lock:
            task = rm.task_graph.get("test::active")
        assert task is not None
        assert task.is_active is False
        blocker.set()

    def test_cancel_missing_task(self, rm: RenderManager):
        """cancel_task returns False for a non-existent task."""
        assert rm.cancel_task("nonexistent::task") is False

    def test_cancel_task_without_event(self, rm: RenderManager):
        """cancel_task returns False when task has no cancel_event."""
        executed = threading.Event()
        rm.submit_task(
            "test::no_evt", Priority.LOW,
            lambda: executed.set(),
        )
        assert rm.cancel_task("test::no_evt") is False
        assert _poll(executed.is_set), "Task without cancel_event should still execute"

    def test_cancelled_task_skips_execution(self, rm: RenderManager):
        """A task cancelled before execution should not call its func."""
        called = threading.Event()
        cancel_evt = threading.Event()
        cancel_evt.set()  # Pre-cancel

        rm.submit_task(
            "test::precancelled", Priority.LOW,
            lambda: called.set(),
            cancel_event=cancel_evt,
        )
        assert not _poll(called.is_set, timeout=0.5), \
            "Cancelled task should not have been executed"

    def test_cancel_event_preserved_on_upgrade(self, rm: RenderManager):
        """Priority upgrade preserves the original cancel_event."""
        evt = threading.Event()
        blocker = threading.Event()
        rm.submit_task(
            "test::upgrade", Priority.LOW,
            lambda: blocker.wait(2),
            cancel_event=evt,
        )
        # Upgrade priority
        rm.submit_task(
            "test::upgrade", Priority.GUI_REQUEST,
            lambda: blocker.wait(2),
            cancel_event=threading.Event(),  # different event
        )
        # The original event should still be on the task
        assert rm.cancel_task("test::upgrade") is True
        assert evt.is_set(), "Original cancel_event should be set after cancel"
        blocker.set()

    def test_cancel_tasks_batch(self, rm: RenderManager):
        """cancel_tasks cancels multiple tasks and returns the count."""
        events = {}
        blocker = threading.Event()
        for i in range(3):
            evt = threading.Event()
            events[f"test::batch{i}"] = evt
            rm.submit_task(
                f"test::batch{i}", Priority.LOW,
                lambda: blocker.wait(2),
                cancel_event=evt,
            )

        count = rm.cancel_tasks(list(events.keys()))
        assert count == 3
        for evt in events.values():
            assert evt.is_set()
        blocker.set()

    def test_cancel_tasks_partial(self, rm: RenderManager):
        """cancel_tasks with a mix of existing and missing tasks."""
        evt = threading.Event()
        blocker = threading.Event()
        rm.submit_task(
            "test::exists", Priority.LOW,
            lambda: blocker.wait(2),
            cancel_event=evt,
        )
        count = rm.cancel_tasks(["test::exists", "test::nope", "test::also_nope"])
        assert count == 1
        assert evt.is_set()
        blocker.set()
