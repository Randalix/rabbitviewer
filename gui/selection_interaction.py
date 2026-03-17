from __future__ import annotations
import time
import logging
from typing import Callable, Optional, Set, TYPE_CHECKING

from core.event_system import event_system, EventType, SelectionChangedEventData
from core.selection import ReplaceSelectionCommand, AddToSelectionCommand, RemoveFromSelectionCommand

if TYPE_CHECKING:
    from gui.thumbnail_model import ThumbnailModel

logger = logging.getLogger(__name__)


class SelectionInteraction:
    """Qt-free selection state machine for thumbnail drag/click/shift selection.

    Uses callbacks for visual preview updates and cursor reset, staying Qt-free.
    """

    def __init__(self, model: ThumbnailModel, label_updater: Callable[[int, bool], None],
                 on_range_end: Optional[Callable[[], None]] = None):
        self.model = model
        self._label_updater = label_updater
        self._on_range_end = on_range_end

        # Drag state
        self._selection_mode: Optional[str] = None
        self._drag_start_index: int = -1
        self._drag_last_index: int = -1
        self._pre_click_selection: Set[str] = set()

        # Shift-to-range state
        self.selection_anchor_index: Optional[int] = None
        self._range_selection_active = False
        self._drag_shift_pending = False

        # Selection tracking (mirrors the central SelectionState)
        self._current_selection: Set[str] = set()
        self._selected_indices: Set[int] = set()
        self._last_preview_selected: Set[int] = set()

        # EventSystem subscriptions
        self._on_shift_pressed_cb = lambda _: self.on_shift_pressed()
        self._on_shift_released_cb = lambda _: self.on_shift_released()
        event_system.subscribe(EventType.SELECTION_CHANGED, self.on_selection_changed)
        event_system.subscribe(EventType.SHIFT_PRESSED, self._on_shift_pressed_cb)
        event_system.subscribe(EventType.SHIFT_RELEASED, self._on_shift_released_cb)

    # -- Mouse event handlers ------------------------------------------------

    def on_mouse_press(self, original_idx: Optional[int], modifiers) -> None:
        from PySide6.QtCore import Qt

        self._pre_click_selection = set(self._current_selection)

        if original_idx is None:
            # Click on background — clear selection
            cmd = ReplaceSelectionCommand(paths=set(), source="thumbnail_view", timestamp=time.time())
            event_system.publish(cmd)
            return

        self._drag_start_index = original_idx
        self._drag_last_index = original_idx

        if modifiers & Qt.ShiftModifier and modifiers & Qt.ControlModifier:
            self._selection_mode = "replace"
        elif modifiers & Qt.ControlModifier:
            self._selection_mode = "remove"
        elif modifiers & Qt.ShiftModifier:
            self._selection_mode = "add"
        else:
            self._selection_mode = "replace"
            self._last_preview_selected = set(self._selected_indices)

        self._update_selection_preview(original_idx, original_idx)

    def on_mouse_move(self, original_idx: Optional[int]) -> None:
        if original_idx is not None:
            self._drag_last_index = original_idx

        if self._range_selection_active and self.selection_anchor_index is not None:
            self._update_selection_preview(self.selection_anchor_index, original_idx)
        elif self._drag_start_index != -1:
            self._update_selection_preview(self._drag_start_index, self._drag_last_index)

    def on_mouse_release(self) -> bool:
        """Returns True if range selection mode was entered (view should set CrossCursor)."""
        if self._drag_start_index == -1:
            return False

        if self._drag_shift_pending:
            self._range_selection_active = True
            self.selection_anchor_index = self._drag_start_index
            self._selection_mode = "add"
            self._drag_start_index = -1
            self._drag_last_index = -1
            self._drag_shift_pending = False
            return True  # signal to view: enter CrossCursor mode
        else:
            self._commit_selection(self._drag_start_index, self._drag_last_index)
            self._drag_start_index = -1
            self._drag_last_index = -1
            self._selection_mode = None
            self._last_preview_selected = set()
            return False

    # -- Shift-key state machine ---------------------------------------------

    def on_shift_pressed(self) -> None:
        if self._drag_start_index != -1 and not self._range_selection_active:
            self._drag_shift_pending = True

    def on_shift_released(self) -> None:
        if self._range_selection_active:
            self._end_range_selection()
        self._drag_shift_pending = False

    def end_range_selection_at(self, end_idx: Optional[int]) -> None:
        """End range selection with a known end index (called by view)."""
        if not self._range_selection_active:
            return
        self._range_selection_active = False
        start = self.selection_anchor_index if self.selection_anchor_index is not None else -1
        if end_idx is None:
            end_idx = start
        self._commit_selection(start, end_idx)
        self.selection_anchor_index = None
        self._selection_mode = None
        if self._on_range_end:
            self._on_range_end()

    def _end_range_selection(self) -> None:
        # why: shift-release fires via EventSystem — SelectionInteraction is
        # Qt-free and cannot query the cursor, so use the last index seen by
        # on_mouse_move as the best available end position.
        end_idx = self._drag_last_index if self._drag_last_index != -1 else self.selection_anchor_index
        self.end_range_selection_at(end_idx)

    # -- Selection state tracking --------------------------------------------

    def on_selection_changed(self, event_data: SelectionChangedEventData) -> None:
        selected_paths = event_data.selected_paths
        new_indices = {self.model.path_to_idx[p] for p in selected_paths if p in self.model.path_to_idx}
        newly_selected = new_indices - self._selected_indices
        deselected = self._selected_indices - new_indices

        for idx in newly_selected:
            self._label_updater(idx, True)
        for idx in deselected:
            self._label_updater(idx, False)

        self._selected_indices = new_indices
        self._current_selection = selected_paths

    def recompute_selected_indices(self) -> None:
        self._selected_indices = {
            self.model.path_to_idx[p] for p in self._current_selection
            if p in self.model.path_to_idx
        }

    def restore_pre_click_selection(self) -> None:
        cmd = ReplaceSelectionCommand(paths=self._pre_click_selection, source="thumbnail_view", timestamp=time.time())
        event_system.publish(cmd)
        self._drag_start_index = -1
        self._drag_last_index = -1
        self._selection_mode = None
        self._last_preview_selected = set()

    # -- Preview and commit --------------------------------------------------

    def _update_selection_preview(self, start_idx: int, end_idx: Optional[int]) -> None:
        if end_idx is None:
            end_idx = start_idx

        preview_indices = self._get_indices_in_range(start_idx, end_idx)

        if self._selection_mode == "replace":
            desired = preview_indices
        elif self._selection_mode == "add":
            desired = self._selected_indices | preview_indices
        elif self._selection_mode == "remove":
            desired = self._selected_indices - preview_indices
        else:
            desired = preview_indices

        to_highlight = desired - self._last_preview_selected
        to_unhighlight = self._last_preview_selected - desired

        for idx in to_highlight:
            self._label_updater(idx, True)
        for idx in to_unhighlight:
            self._label_updater(idx, False)

        self._last_preview_selected = desired

    def _commit_selection(self, start_idx: int, end_idx: int) -> None:
        paths_in_range = self._get_paths_in_range(start_idx, end_idx)
        command = None

        if self._selection_mode == "replace":
            if start_idx == end_idx and paths_in_range <= self._current_selection:
                paths_in_range = set()
            command = ReplaceSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "add":
            command = AddToSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())
        elif self._selection_mode == "remove":
            command = RemoveFromSelectionCommand(paths=paths_in_range, source="thumbnail_view", timestamp=time.time())

        if command:
            event_system.publish(command)
            self.selection_anchor_index = end_idx

    def _get_indices_in_range(self, start_idx: int, end_idx: int) -> Set[int]:
        start_pos = self.model.original_to_visible.get(start_idx, -1)
        end_pos = self.model.original_to_visible.get(end_idx, -1)
        if start_pos == -1 or end_pos == -1:
            return {start_idx} if start_idx != -1 else set()

        low, high = min(start_pos, end_pos), max(start_pos, end_pos)
        return {self.model.visible_original_indices[i] for i in range(low, high + 1)}

    def _get_paths_in_range(self, start_idx: int, end_idx: int) -> Set[str]:
        return {self.model.all_files[idx] for idx in self._get_indices_in_range(start_idx, end_idx)}

    # -- Query ---------------------------------------------------------------

    @property
    def current_selection(self) -> Set[str]:
        return self._current_selection

    @property
    def is_in_drag(self) -> bool:
        return self._drag_start_index != -1

    @property
    def is_range_selection_active(self) -> bool:
        return self._range_selection_active

    def is_selected(self, orig_idx: int) -> bool:
        if self._drag_start_index != -1 and self._last_preview_selected:
            return orig_idx in self._last_preview_selected
        return orig_idx in self._selected_indices

    # -- Lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        """Reset all selection state (called on clear_layout)."""
        self.selection_anchor_index = None
        self._range_selection_active = False
        self._drag_shift_pending = False
        self._selection_mode = None
        self._drag_start_index = -1
        self._drag_last_index = -1
        self._current_selection = set()
        self._selected_indices = set()
        self._last_preview_selected = set()
        self._pre_click_selection = set()

    def dispose(self) -> None:
        """Unsubscribe from events (called on closeEvent)."""
        event_system.unsubscribe(EventType.SELECTION_CHANGED, self.on_selection_changed)
        event_system.unsubscribe(EventType.SHIFT_PRESSED, self._on_shift_pressed_cb)
        event_system.unsubscribe(EventType.SHIFT_RELEASED, self._on_shift_released_cb)
