"""Tests for the selection system: state, commands, processor, history, and event integration."""
import os
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

# Mock PySide6 before any project imports — EventSystem inherits QObject.
_qt_core = types.ModuleType("PySide6.QtCore")
_qt_core.QObject = object
_qt_core.QPointF = MagicMock
sys.modules.setdefault("PySide6", types.ModuleType("PySide6"))
sys.modules.setdefault("PySide6.QtCore", _qt_core)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.event_system import EventSystem, EventType, SelectionChangedEventData
from core.selection import (
    AddToSelectionCommand,
    RemoveFromSelectionCommand,
    ReplaceSelectionCommand,
    SelectionHistory,
    SelectionProcessor,
    SelectionState,
    ToggleSelectionCommand,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_event_system(monkeypatch):
    """Replace the module-level singleton so tests never leak subscribers."""
    fresh = EventSystem()
    monkeypatch.setattr("core.event_system.event_system", fresh)
    monkeypatch.setattr("core.selection.event_system", fresh)
    return fresh


@pytest.fixture()
def state():
    return SelectionState()


@pytest.fixture()
def processor(state, fresh_event_system):
    return SelectionProcessor(state)


@pytest.fixture()
def history(processor, fresh_event_system):
    return SelectionHistory(processor)


def _cmd(cls, paths, source="test"):
    return cls(paths=set(paths), source=source, timestamp=time.time())


# ===================================================================
# SelectionState
# ===================================================================

class TestSelectionState:
    def test_initial_empty(self, state):
        assert state.selected_paths == set()

    def test_set_selection(self, state):
        state.set_selection({"a.jpg", "b.jpg", "c.jpg"})
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_set_selection_replaces(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        state.set_selection({"c.jpg", "d.jpg"})
        assert state.selected_paths == {"c.jpg", "d.jpg"}

    def test_add_to_selection(self, state):
        state.set_selection({"a.jpg"})
        state.add_to_selection({"b.jpg", "c.jpg"})
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_add_to_selection_idempotent(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        state.add_to_selection({"b.jpg", "c.jpg"})
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_remove_from_selection(self, state):
        state.set_selection({"a.jpg", "b.jpg", "c.jpg"})
        state.remove_from_selection({"b.jpg"})
        assert state.selected_paths == {"a.jpg", "c.jpg"}

    def test_remove_nonexistent_noop(self, state):
        state.set_selection({"a.jpg"})
        state.remove_from_selection({"z.jpg"})
        assert state.selected_paths == {"a.jpg"}

    def test_remove_all(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        state.remove_from_selection({"a.jpg", "b.jpg"})
        assert state.selected_paths == set()


# ===================================================================
# Commands — execute / undo
# ===================================================================

class TestReplaceSelectionCommand:
    def test_execute(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, ["e.jpg", "f.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"e.jpg", "f.jpg"}

    def test_undo_restores(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, ["e.jpg", "f.jpg"])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_replace_with_empty_clears(self, state):
        state.set_selection({"a.jpg", "b.jpg", "c.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_paths == set()

    def test_undo_after_clear_restores(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, [])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}


class TestAddToSelectionCommand:
    def test_execute(self, state):
        state.set_selection({"a.jpg"})
        cmd = _cmd(AddToSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_undo_restores(self, state):
        state.set_selection({"a.jpg"})
        cmd = _cmd(AddToSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg"}

    def test_add_overlapping(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(AddToSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_add_to_empty(self, state):
        cmd = _cmd(AddToSelectionCommand, ["d.jpg", "e.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"d.jpg", "e.jpg"}


class TestRemoveFromSelectionCommand:
    def test_execute(self, state):
        state.set_selection({"a.jpg", "b.jpg", "c.jpg"})
        cmd = _cmd(RemoveFromSelectionCommand, ["b.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "c.jpg"}

    def test_undo_restores(self, state):
        state.set_selection({"a.jpg", "b.jpg", "c.jpg"})
        cmd = _cmd(RemoveFromSelectionCommand, ["b.jpg"])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_remove_nonexistent(self, state):
        state.set_selection({"a.jpg"})
        cmd = _cmd(RemoveFromSelectionCommand, ["z.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg"}

    def test_remove_all_items(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(RemoveFromSelectionCommand, ["a.jpg", "b.jpg"])
        cmd.execute(state)
        assert state.selected_paths == set()


class TestToggleSelectionCommand:
    def test_toggle_adds_new(self, state):
        state.set_selection({"a.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_toggle_removes_existing(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg"}

    def test_toggle_mixed(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "c.jpg"}

    def test_undo_restores_previous(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "c.jpg"}
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_undo_robust_to_external_mutation(self, state):
        """Toggle undo restores previous_selection even if state was externally modified."""
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "c.jpg"}
        # External mutation between execute and undo
        state.add_to_selection({"z.jpg"})
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_double_toggle_roundtrip(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ToggleSelectionCommand, ["b.jpg", "c.jpg"])
        cmd.execute(state)
        cmd.undo(state)
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}


# ===================================================================
# SelectionProcessor
# ===================================================================

class TestSelectionProcessor:
    def test_process_command_modifies_state(self, processor, state):
        cmd = _cmd(ReplaceSelectionCommand, ["x.jpg", "y.jpg"])
        processor.process_command(cmd)
        assert state.selected_paths == {"x.jpg", "y.jpg"}

    def test_process_command_publishes_event(self, processor, fresh_event_system):
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        processor.process_command(cmd)
        assert len(received) == 1
        assert isinstance(received[0], SelectionChangedEventData)
        assert received[0].selected_paths == {"a.jpg"}

    def test_process_undo(self, processor, state):
        state.set_selection({"e.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, ["x.jpg"])
        processor.process_command(cmd)
        assert state.selected_paths == {"x.jpg"}
        processor.process_command(cmd, is_undo=True)
        assert state.selected_paths == {"e.jpg"}

    def test_on_new_command_via_event_bus(self, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, ["q.jpg"])
        fresh_event_system.publish(cmd)
        assert state.selected_paths == {"q.jpg"}

    def test_published_paths_are_frozenset(self, processor, state, fresh_event_system):
        """Published selection must be a frozenset to prevent downstream aliasing."""
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"])
        processor.process_command(cmd)
        assert isinstance(received[0].selected_paths, frozenset)
        assert received[0].selected_paths == frozenset({"a.jpg", "b.jpg"})

    def test_published_paths_immutable_after_state_change(self, processor, state, fresh_event_system):
        """Mutating the state after publish must not affect delivered event data."""
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"])
        processor.process_command(cmd)
        # Mutate state after publish
        state.set_selection(set())
        assert received[0].selected_paths == frozenset({"a.jpg", "b.jpg"})


# ===================================================================
# SelectionHistory — undo / redo
# ===================================================================

class TestSelectionHistory:
    def test_undo_single(self, history, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        fresh_event_system.publish(cmd)
        assert state.selected_paths == {"a.jpg"}
        history.undo()
        assert state.selected_paths == set()

    def test_redo_single(self, history, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        fresh_event_system.publish(cmd)
        history.undo()
        assert state.selected_paths == set()
        history.redo()
        assert state.selected_paths == {"a.jpg"}

    def test_multiple_undo(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["b.jpg"]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["c.jpg"]))
        assert state.selected_paths == {"c.jpg"}
        history.undo()
        assert state.selected_paths == {"b.jpg"}
        history.undo()
        assert state.selected_paths == {"a.jpg"}
        history.undo()
        assert state.selected_paths == set()

    def test_undo_redo_interleaved(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["b.jpg"]))
        history.undo()
        assert state.selected_paths == {"a.jpg"}
        history.redo()
        assert state.selected_paths == {"b.jpg"}

    def test_new_command_clears_redo(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["b.jpg"]))
        history.undo()
        assert state.selected_paths == {"a.jpg"}
        # New command should clear redo stack
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["z.jpg"]))
        assert state.selected_paths == {"z.jpg"}
        history.redo()  # Should be a no-op
        assert state.selected_paths == {"z.jpg"}

    def test_undo_empty_stack_noop(self, history, state):
        history.undo()
        assert state.selected_paths == set()

    def test_redo_empty_stack_noop(self, history, state):
        history.redo()
        assert state.selected_paths == set()

    def test_undo_add_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, ["c.jpg"]))
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}
        history.undo()
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_undo_remove_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg", "c.jpg"]))
        fresh_event_system.publish(_cmd(RemoveFromSelectionCommand, ["b.jpg"]))
        assert state.selected_paths == {"a.jpg", "c.jpg"}
        history.undo()
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

    def test_undo_toggle_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"]))
        fresh_event_system.publish(_cmd(ToggleSelectionCommand, ["b.jpg", "c.jpg"]))
        assert state.selected_paths == {"a.jpg", "c.jpg"}
        history.undo()
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_full_undo_redo_cycle(self, history, processor, state, fresh_event_system):
        """Undo everything, then redo the most recent undo.

        redo() re-publishes the command, which triggers on_command_executed
        and clears the remaining redo stack.  Only the most recent undo can
        be redone before the redo stack is wiped.
        """
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, ["b.jpg"]))
        fresh_event_system.publish(_cmd(ToggleSelectionCommand, ["a.jpg", "c.jpg"]))
        assert state.selected_paths == {"b.jpg", "c.jpg"}

        history.undo()  # undo toggle → {a.jpg, b.jpg}
        assert state.selected_paths == {"a.jpg", "b.jpg"}
        history.undo()  # undo add → {a.jpg}
        assert state.selected_paths == {"a.jpg"}
        history.undo()  # undo replace → {}
        assert state.selected_paths == set()

        history.redo()  # redo replace → {a.jpg}
        assert state.selected_paths == {"a.jpg"}
        # redo stack is now empty (cleared by on_command_executed)
        history.redo()  # no-op
        assert state.selected_paths == {"a.jpg"}

    def test_single_undo_redo_pair(self, history, processor, state, fresh_event_system):
        """The typical undo-then-redo workflow works correctly."""
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, ["b.jpg", "c.jpg"]))
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}

        history.undo()
        assert state.selected_paths == {"a.jpg"}
        history.redo()
        assert state.selected_paths == {"a.jpg", "b.jpg", "c.jpg"}


# ===================================================================
# Event System integration
# ===================================================================

class TestEventSystemIntegration:
    def test_selection_changed_fires_on_command(self, processor, fresh_event_system):
        events = []
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, events.append)
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"])
        fresh_event_system.publish(cmd)
        assert len(events) == 1
        assert events[0].selected_paths == {"a.jpg", "b.jpg"}

    def test_selection_changed_fires_on_undo(self, processor, history, fresh_event_system):
        events = []
        cmd = _cmd(ReplaceSelectionCommand, ["e.jpg"])
        fresh_event_system.publish(cmd)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, events.append)
        history.undo()
        assert len(events) == 1
        assert events[0].selected_paths == set()

    def test_subscriber_error_does_not_break_others(self, fresh_event_system, processor):
        """A crashing subscriber must not prevent other subscribers from running."""
        results = []

        def bad_handler(e):
            raise RuntimeError("boom")

        def good_handler(e):
            results.append(e)

        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, bad_handler)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, good_handler)

        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        processor.process_command(cmd)
        assert len(results) == 1

    def test_event_history_recorded(self, processor, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        processor.process_command(cmd)
        history = fresh_event_system.get_event_history(EventType.SELECTION_CHANGED)
        assert len(history) == 1

    def test_unsubscribe(self, fresh_event_system, processor):
        calls = []
        handler = lambda e: calls.append(e)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, handler)
        processor.process_command(_cmd(ReplaceSelectionCommand, ["a.jpg"]))
        assert len(calls) == 1
        fresh_event_system.unsubscribe(EventType.SELECTION_CHANGED, handler)
        processor.process_command(_cmd(ReplaceSelectionCommand, ["b.jpg"]))
        assert len(calls) == 1  # no new call


# ===================================================================
# Command EventData properties
# ===================================================================

class TestCommandMetadata:
    def test_command_has_event_type(self):
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        assert cmd.event_type == EventType.EXECUTE_SELECTION_COMMAND

    def test_command_has_source(self):
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"], source="thumbnail_view")
        assert cmd.source == "thumbnail_view"

    def test_command_has_timestamp(self):
        before = time.time()
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg"])
        after = time.time()
        assert before <= cmd.timestamp <= after

    def test_previous_selection_set_on_execute(self, state):
        state.set_selection({"x.jpg", "y.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, ["z.jpg"])
        cmd.execute(state)
        assert cmd.previous_selection == {"x.jpg", "y.jpg"}


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:
    def test_large_selection(self, state):
        paths = {f"img_{i}.jpg" for i in range(10_000)}
        state.set_selection(paths)
        assert len(state.selected_paths) == 10_000

    def test_replace_with_same(self, state):
        state.set_selection({"a.jpg", "b.jpg"})
        cmd = _cmd(ReplaceSelectionCommand, ["a.jpg", "b.jpg"])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg", "b.jpg"}

    def test_toggle_empty_on_empty(self, state):
        cmd = _cmd(ToggleSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_paths == set()

    def test_add_empty_to_selection(self, state):
        state.set_selection({"a.jpg"})
        cmd = _cmd(AddToSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_paths == {"a.jpg"}

    def test_rapid_command_sequence(self, processor, state, fresh_event_system):
        """Simulate rapid clicks — many commands in quick succession."""
        for i in range(100):
            fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [f"img_{i}.jpg"]))
        assert state.selected_paths == {"img_99.jpg"}

    def test_undo_redo_rapid(self, history, processor, state, fresh_event_system):
        for i in range(50):
            fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [f"img_{i}.jpg"]))
        for _ in range(50):
            history.undo()
        assert state.selected_paths == set()
        # Only the most recent undo can be redone (redo re-publishes, clearing redo stack)
        history.redo()
        assert state.selected_paths == {"img_0.jpg"}
