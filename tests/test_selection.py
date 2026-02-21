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


def _cmd(cls, indices, source="test"):
    return cls(indices=set(indices), source=source, timestamp=time.time())


# ===================================================================
# SelectionState
# ===================================================================

class TestSelectionState:
    def test_initial_empty(self, state):
        assert state.selected_indices == set()

    def test_set_selection(self, state):
        state.set_selection({1, 2, 3})
        assert state.selected_indices == {1, 2, 3}

    def test_set_selection_replaces(self, state):
        state.set_selection({1, 2})
        state.set_selection({3, 4})
        assert state.selected_indices == {3, 4}

    def test_add_to_selection(self, state):
        state.set_selection({1})
        state.add_to_selection({2, 3})
        assert state.selected_indices == {1, 2, 3}

    def test_add_to_selection_idempotent(self, state):
        state.set_selection({1, 2})
        state.add_to_selection({2, 3})
        assert state.selected_indices == {1, 2, 3}

    def test_remove_from_selection(self, state):
        state.set_selection({1, 2, 3})
        state.remove_from_selection({2})
        assert state.selected_indices == {1, 3}

    def test_remove_nonexistent_noop(self, state):
        state.set_selection({1})
        state.remove_from_selection({99})
        assert state.selected_indices == {1}

    def test_remove_all(self, state):
        state.set_selection({1, 2})
        state.remove_from_selection({1, 2})
        assert state.selected_indices == set()


# ===================================================================
# Commands — execute / undo
# ===================================================================

class TestReplaceSelectionCommand:
    def test_execute(self, state):
        state.set_selection({0, 1})
        cmd = _cmd(ReplaceSelectionCommand, [5, 6])
        cmd.execute(state)
        assert state.selected_indices == {5, 6}

    def test_undo_restores(self, state):
        state.set_selection({0, 1})
        cmd = _cmd(ReplaceSelectionCommand, [5, 6])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_indices == {0, 1}

    def test_replace_with_empty_clears(self, state):
        state.set_selection({1, 2, 3})
        cmd = _cmd(ReplaceSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_indices == set()

    def test_undo_after_clear_restores(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ReplaceSelectionCommand, [])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_indices == {1, 2}


class TestAddToSelectionCommand:
    def test_execute(self, state):
        state.set_selection({1})
        cmd = _cmd(AddToSelectionCommand, [2, 3])
        cmd.execute(state)
        assert state.selected_indices == {1, 2, 3}

    def test_undo_restores(self, state):
        state.set_selection({1})
        cmd = _cmd(AddToSelectionCommand, [2, 3])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_indices == {1}

    def test_add_overlapping(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(AddToSelectionCommand, [2, 3])
        cmd.execute(state)
        assert state.selected_indices == {1, 2, 3}

    def test_add_to_empty(self, state):
        cmd = _cmd(AddToSelectionCommand, [4, 5])
        cmd.execute(state)
        assert state.selected_indices == {4, 5}


class TestRemoveFromSelectionCommand:
    def test_execute(self, state):
        state.set_selection({1, 2, 3})
        cmd = _cmd(RemoveFromSelectionCommand, [2])
        cmd.execute(state)
        assert state.selected_indices == {1, 3}

    def test_undo_restores(self, state):
        state.set_selection({1, 2, 3})
        cmd = _cmd(RemoveFromSelectionCommand, [2])
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_indices == {1, 2, 3}

    def test_remove_nonexistent(self, state):
        state.set_selection({1})
        cmd = _cmd(RemoveFromSelectionCommand, [99])
        cmd.execute(state)
        assert state.selected_indices == {1}

    def test_remove_all_items(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(RemoveFromSelectionCommand, [1, 2])
        cmd.execute(state)
        assert state.selected_indices == set()


class TestToggleSelectionCommand:
    def test_toggle_adds_new(self, state):
        state.set_selection({1})
        cmd = _cmd(ToggleSelectionCommand, [2])
        cmd.execute(state)
        assert state.selected_indices == {1, 2}

    def test_toggle_removes_existing(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ToggleSelectionCommand, [2])
        cmd.execute(state)
        assert state.selected_indices == {1}

    def test_toggle_mixed(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ToggleSelectionCommand, [2, 3])
        cmd.execute(state)
        assert state.selected_indices == {1, 3}

    def test_undo_restores_previous(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ToggleSelectionCommand, [2, 3])
        cmd.execute(state)
        assert state.selected_indices == {1, 3}
        cmd.undo(state)
        assert state.selected_indices == {1, 2}

    def test_undo_robust_to_external_mutation(self, state):
        """Toggle undo restores previous_selection even if state was externally modified."""
        state.set_selection({1, 2})
        cmd = _cmd(ToggleSelectionCommand, [2, 3])
        cmd.execute(state)
        assert state.selected_indices == {1, 3}
        # External mutation between execute and undo
        state.add_to_selection({99})
        cmd.undo(state)
        assert state.selected_indices == {1, 2}

    def test_double_toggle_roundtrip(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ToggleSelectionCommand, [2, 3])
        cmd.execute(state)
        cmd.undo(state)
        cmd.execute(state)
        cmd.undo(state)
        assert state.selected_indices == {1, 2}


# ===================================================================
# SelectionProcessor
# ===================================================================

class TestSelectionProcessor:
    def test_process_command_modifies_state(self, processor, state):
        cmd = _cmd(ReplaceSelectionCommand, [10, 20])
        processor.process_command(cmd)
        assert state.selected_indices == {10, 20}

    def test_process_command_publishes_event(self, processor, fresh_event_system):
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, [1])
        processor.process_command(cmd)
        assert len(received) == 1
        assert isinstance(received[0], SelectionChangedEventData)
        assert received[0].selected_indices == {1}

    def test_process_undo(self, processor, state):
        state.set_selection({5})
        cmd = _cmd(ReplaceSelectionCommand, [10])
        processor.process_command(cmd)
        assert state.selected_indices == {10}
        processor.process_command(cmd, is_undo=True)
        assert state.selected_indices == {5}

    def test_on_new_command_via_event_bus(self, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, [42])
        fresh_event_system.publish(cmd)
        assert state.selected_indices == {42}

    def test_published_indices_are_frozenset(self, processor, state, fresh_event_system):
        """Published selection must be a frozenset to prevent downstream aliasing."""
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, [1, 2])
        processor.process_command(cmd)
        assert isinstance(received[0].selected_indices, frozenset)
        assert received[0].selected_indices == frozenset({1, 2})

    def test_published_indices_immutable_after_state_change(self, processor, state, fresh_event_system):
        """Mutating the state after publish must not affect delivered event data."""
        received = []
        fresh_event_system.subscribe(
            EventType.SELECTION_CHANGED,
            lambda e: received.append(e),
        )
        cmd = _cmd(ReplaceSelectionCommand, [1, 2])
        processor.process_command(cmd)
        # Mutate state after publish
        state.set_selection(set())
        assert received[0].selected_indices == frozenset({1, 2})


# ===================================================================
# SelectionHistory — undo / redo
# ===================================================================

class TestSelectionHistory:
    def test_undo_single(self, history, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, [1])
        fresh_event_system.publish(cmd)
        assert state.selected_indices == {1}
        history.undo()
        assert state.selected_indices == set()

    def test_redo_single(self, history, processor, state, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, [1])
        fresh_event_system.publish(cmd)
        history.undo()
        assert state.selected_indices == set()
        history.redo()
        assert state.selected_indices == {1}

    def test_multiple_undo(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [2]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [3]))
        assert state.selected_indices == {3}
        history.undo()
        assert state.selected_indices == {2}
        history.undo()
        assert state.selected_indices == {1}
        history.undo()
        assert state.selected_indices == set()

    def test_undo_redo_interleaved(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [2]))
        history.undo()
        assert state.selected_indices == {1}
        history.redo()
        assert state.selected_indices == {2}

    def test_new_command_clears_redo(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1]))
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [2]))
        history.undo()
        assert state.selected_indices == {1}
        # New command should clear redo stack
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [99]))
        assert state.selected_indices == {99}
        history.redo()  # Should be a no-op
        assert state.selected_indices == {99}

    def test_undo_empty_stack_noop(self, history, state):
        history.undo()
        assert state.selected_indices == set()

    def test_redo_empty_stack_noop(self, history, state):
        history.redo()
        assert state.selected_indices == set()

    def test_undo_add_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1, 2]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, [3]))
        assert state.selected_indices == {1, 2, 3}
        history.undo()
        assert state.selected_indices == {1, 2}

    def test_undo_remove_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1, 2, 3]))
        fresh_event_system.publish(_cmd(RemoveFromSelectionCommand, [2]))
        assert state.selected_indices == {1, 3}
        history.undo()
        assert state.selected_indices == {1, 2, 3}

    def test_undo_toggle_command(self, history, processor, state, fresh_event_system):
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1, 2]))
        fresh_event_system.publish(_cmd(ToggleSelectionCommand, [2, 3]))
        assert state.selected_indices == {1, 3}
        history.undo()
        assert state.selected_indices == {1, 2}

    def test_full_undo_redo_cycle(self, history, processor, state, fresh_event_system):
        """Undo everything, then redo the most recent undo.

        redo() re-publishes the command, which triggers on_command_executed
        and clears the remaining redo stack.  Only the most recent undo can
        be redone before the redo stack is wiped.
        """
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, [2]))
        fresh_event_system.publish(_cmd(ToggleSelectionCommand, [1, 3]))
        assert state.selected_indices == {2, 3}

        history.undo()  # undo toggle → {1, 2}
        assert state.selected_indices == {1, 2}
        history.undo()  # undo add → {1}
        assert state.selected_indices == {1}
        history.undo()  # undo replace → {}
        assert state.selected_indices == set()

        history.redo()  # redo replace → {1}
        assert state.selected_indices == {1}
        # redo stack is now empty (cleared by on_command_executed)
        history.redo()  # no-op
        assert state.selected_indices == {1}

    def test_single_undo_redo_pair(self, history, processor, state, fresh_event_system):
        """The typical undo-then-redo workflow works correctly."""
        fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [1]))
        fresh_event_system.publish(_cmd(AddToSelectionCommand, [2, 3]))
        assert state.selected_indices == {1, 2, 3}

        history.undo()
        assert state.selected_indices == {1}
        history.redo()
        assert state.selected_indices == {1, 2, 3}


# ===================================================================
# Event System integration
# ===================================================================

class TestEventSystemIntegration:
    def test_selection_changed_fires_on_command(self, processor, fresh_event_system):
        events = []
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, events.append)
        cmd = _cmd(ReplaceSelectionCommand, [1, 2])
        fresh_event_system.publish(cmd)
        assert len(events) == 1
        assert events[0].selected_indices == {1, 2}

    def test_selection_changed_fires_on_undo(self, processor, history, fresh_event_system):
        events = []
        cmd = _cmd(ReplaceSelectionCommand, [5])
        fresh_event_system.publish(cmd)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, events.append)
        history.undo()
        assert len(events) == 1
        assert events[0].selected_indices == set()

    def test_subscriber_error_does_not_break_others(self, fresh_event_system, processor):
        """A crashing subscriber must not prevent other subscribers from running."""
        results = []

        def bad_handler(e):
            raise RuntimeError("boom")

        def good_handler(e):
            results.append(e)

        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, bad_handler)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, good_handler)

        cmd = _cmd(ReplaceSelectionCommand, [1])
        processor.process_command(cmd)
        assert len(results) == 1

    def test_event_history_recorded(self, processor, fresh_event_system):
        cmd = _cmd(ReplaceSelectionCommand, [1])
        processor.process_command(cmd)
        history = fresh_event_system.get_event_history(EventType.SELECTION_CHANGED)
        assert len(history) == 1

    def test_unsubscribe(self, fresh_event_system, processor):
        calls = []
        handler = lambda e: calls.append(e)
        fresh_event_system.subscribe(EventType.SELECTION_CHANGED, handler)
        processor.process_command(_cmd(ReplaceSelectionCommand, [1]))
        assert len(calls) == 1
        fresh_event_system.unsubscribe(EventType.SELECTION_CHANGED, handler)
        processor.process_command(_cmd(ReplaceSelectionCommand, [2]))
        assert len(calls) == 1  # no new call


# ===================================================================
# Command EventData properties
# ===================================================================

class TestCommandMetadata:
    def test_command_has_event_type(self):
        cmd = _cmd(ReplaceSelectionCommand, [1])
        assert cmd.event_type == EventType.EXECUTE_SELECTION_COMMAND

    def test_command_has_source(self):
        cmd = _cmd(ReplaceSelectionCommand, [1], source="thumbnail_view")
        assert cmd.source == "thumbnail_view"

    def test_command_has_timestamp(self):
        before = time.time()
        cmd = _cmd(ReplaceSelectionCommand, [1])
        after = time.time()
        assert before <= cmd.timestamp <= after

    def test_previous_selection_set_on_execute(self, state):
        state.set_selection({10, 20})
        cmd = _cmd(ReplaceSelectionCommand, [30])
        cmd.execute(state)
        assert cmd.previous_selection == {10, 20}


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:
    def test_large_selection(self, state):
        indices = set(range(10_000))
        state.set_selection(indices)
        assert len(state.selected_indices) == 10_000

    def test_replace_with_same(self, state):
        state.set_selection({1, 2})
        cmd = _cmd(ReplaceSelectionCommand, [1, 2])
        cmd.execute(state)
        assert state.selected_indices == {1, 2}

    def test_toggle_empty_on_empty(self, state):
        cmd = _cmd(ToggleSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_indices == set()

    def test_add_empty_to_selection(self, state):
        state.set_selection({1})
        cmd = _cmd(AddToSelectionCommand, [])
        cmd.execute(state)
        assert state.selected_indices == {1}

    def test_rapid_command_sequence(self, processor, state, fresh_event_system):
        """Simulate rapid clicks — many commands in quick succession."""
        for i in range(100):
            fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [i]))
        assert state.selected_indices == {99}

    def test_undo_redo_rapid(self, history, processor, state, fresh_event_system):
        for i in range(50):
            fresh_event_system.publish(_cmd(ReplaceSelectionCommand, [i]))
        for _ in range(50):
            history.undo()
        assert state.selected_indices == set()
        # Only the most recent undo can be redone (redo re-publishes, clearing redo stack)
        history.redo()
        assert state.selected_indices == {0}
