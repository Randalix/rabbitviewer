import logging
import time
from abc import ABC, abstractmethod
from typing import Set, List

from .event_system import EventData, EventType, SelectionChangedEventData, event_system


class SelectionState:
    """Holds the current set of selected item indices as the single source of truth."""

    def __init__(self):
        self.selected_indices: Set[int] = set()

    def set_selection(self, indices: Set[int]):
        self.selected_indices = indices

    def add_to_selection(self, indices: Set[int]):
        self.selected_indices.update(indices)

    def remove_from_selection(self, indices: Set[int]):
        self.selected_indices.difference_update(indices)


class SelectionCommand(EventData, ABC):
    """Abstract base class for selection commands, inheriting from EventData to be publishable."""

    def __init__(self, indices: Set[int], source: str, timestamp: float):
        super().__init__(event_type=EventType.EXECUTE_SELECTION_COMMAND, source=source, timestamp=timestamp)
        self.indices = indices
        self.previous_selection: Set[int] = set()

    @abstractmethod
    def execute(self, state: SelectionState) -> None:
        """Executes the command, modifying the selection state."""
        pass

    @abstractmethod
    def undo(self, state: SelectionState) -> None:
        """Reverts the command, restoring the previous selection state."""
        pass


class ReplaceSelectionCommand(SelectionCommand):
    """Command to replace the entire selection."""

    def execute(self, state: SelectionState) -> None:
        self.previous_selection = state.selected_indices.copy()
        state.set_selection(self.indices)
        logging.debug(f"Executed ReplaceSelection: {self.indices}")

    def undo(self, state: SelectionState) -> None:
        state.set_selection(self.previous_selection)
        logging.debug(f"Undid ReplaceSelection, restored: {self.previous_selection}")


class AddToSelectionCommand(SelectionCommand):
    """Command to add items to the current selection."""

    def execute(self, state: SelectionState) -> None:
        self.previous_selection = state.selected_indices.copy()
        state.add_to_selection(self.indices)
        logging.debug(f"Executed AddToSelection: {self.indices}")

    def undo(self, state: SelectionState) -> None:
        state.set_selection(self.previous_selection)
        logging.debug(f"Undid AddToSelection, restored: {self.previous_selection}")


class RemoveFromSelectionCommand(SelectionCommand):
    """Command to remove items from the current selection."""

    def execute(self, state: SelectionState) -> None:
        self.previous_selection = state.selected_indices.copy()
        state.remove_from_selection(self.indices)
        logging.debug(f"Executed RemoveFromSelection: {self.indices}")

    def undo(self, state: SelectionState) -> None:
        state.set_selection(self.previous_selection)
        logging.debug(f"Undid RemoveFromSelection, restored: {self.previous_selection}")


class ToggleSelectionCommand(SelectionCommand):
    """Command to toggle items in the selection (XOR operation)."""

    def execute(self, state: SelectionState) -> None:
        self.previous_selection = state.selected_indices.copy()
        state.selected_indices.symmetric_difference_update(self.indices)
        logging.debug(f"Executed ToggleSelection: {self.indices}")

    def undo(self, state: SelectionState) -> None:
        # Symmetric difference is its own inverse.
        state.selected_indices.symmetric_difference_update(self.indices)
        logging.debug(f"Undid ToggleSelection: {self.indices}")


class SelectionProcessor:
    """Executes selection commands, modifies SelectionState, and publishes changes."""

    def __init__(self, state: SelectionState):
        self.state = state
        event_system.subscribe(EventType.EXECUTE_SELECTION_COMMAND, self.on_new_command)

    def on_new_command(self, command: SelectionCommand):
        """Handler for new commands from the event bus that are not undos/redos."""
        if isinstance(command, SelectionCommand):
            self.process_command(command, is_undo=False)

    def process_command(self, command: SelectionCommand, is_undo: bool = False):
        """Applies or undoes a command and publishes the result."""
        if is_undo:
            command.undo(self.state)
        else:
            command.execute(self.state)

        # Publish the final state change
        final_selection = self.state.selected_indices.copy()
        change_event = SelectionChangedEventData(
            event_type=EventType.SELECTION_CHANGED,
            source="SelectionProcessor",
            timestamp=time.time(),
            selected_indices=final_selection
        )
        event_system.publish(change_event)
        logging.debug(f"Published SELECTION_CHANGED with {len(final_selection)} items.")


class SelectionHistory:
    """Manages undo/redo stacks for selection commands."""

    def __init__(self, processor: SelectionProcessor):
        self.processor = processor
        self.undo_stack: List[SelectionCommand] = []
        self.redo_stack: List[SelectionCommand] = []
        event_system.subscribe(EventType.EXECUTE_SELECTION_COMMAND, self.on_command_executed)

    def on_command_executed(self, command: SelectionCommand):
        """Adds a command to the undo stack and clears the redo stack."""
        if isinstance(command, SelectionCommand):
            self.undo_stack.append(command)
            self.redo_stack.clear()
            logging.debug(f"Pushed to undo stack. Size: {len(self.undo_stack)}")

    def undo(self):
        """Undoes the last command and moves it to the redo stack."""
        if self.undo_stack:
            command = self.undo_stack.pop()
            # Process the command as an undo, which will trigger a SELECTION_CHANGED event
            self.processor.process_command(command, is_undo=True)
            self.redo_stack.append(command)
            logging.info(f"Undoing command: {type(command).__name__}")

    def redo(self):
        """Redoes the last undone command."""
        if self.redo_stack:
            command = self.redo_stack.pop()
            # Re-executing the command will fire a new EXECUTE_SELECTION_COMMAND event,
            # which our `on_command_executed` handler will pick up to put it back on the undo stack.
            event_system.publish(command)
            logging.info(f"Redoing command: {type(command).__name__}")
