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

    def test_cancel_missing_task(self, rm: RenderManager):
        """cancel_task returns False for a non-existent task."""
        assert rm.cancel_task("nonexistent::task") is False

    def test_cancel_task_without_event(self, rm: RenderManager):
        """cancel_task returns False when task has no cancel_event."""
        blocker = threading.Event()
        rm.submit_task(
            "test::no_evt", Priority.LOW,
            lambda: blocker.wait(2),
        )
        assert rm.cancel_task("test::no_evt") is False
        blocker.set()

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
        time.sleep(0.5)
        assert not called.is_set(), "Cancelled task should not have been executed"
