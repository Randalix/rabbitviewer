from __future__ import annotations
import bisect
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from core.file_grouping import FileGroup, build_group_map
from core.folder_node import FolderNode

logger = logging.getLogger(__name__)


@dataclass
class ImageState:
    loaded: bool = False
    prioritized: bool = False


@dataclass
class AddBatchResult:
    new_files: List[str]
    start_idx: int


@dataclass
class RemoveResult:
    removed_vis_indices: Set[int]
    removed_paths: Set[str]


class ThumbnailModel:

    def __init__(self):
        # Core data
        self.all_files: List[str] = []
        self.all_files_set: Set[str] = set()
        self.current_files: List[str] = []
        self.path_to_idx: Dict[str, int] = {}

        # Visible ↔ original index mappings
        self.visible_to_original: Dict[int, int] = {}
        self.original_to_visible: Dict[int, int] = {}
        self.visible_original_indices: List[int] = []
        self.hidden_indices: Set[int] = set()

        # Per-index state and caches
        self.image_states: Dict[int, ImageState] = {}
        self.pixmap_cache: Dict[int, object] = {}
        self.thumb_path_cache: Dict[int, str] = {}
        self.initial_thumb_paths: Dict[str, str] = {}

        # Directory and scan state
        self.current_directory_path: Optional[str] = None
        self.scan_active: bool = False
        self.last_layout_file_count: int = 0

        # Filter state
        self.current_filter: str = ""
        self.current_star_filter: List[bool] = [True, True, True, True, True, True]
        self.current_tag_filter: List[str] = []
        self.clip_search_paths: Optional[Set[str]] = None
        self.person_filter_paths: Optional[Set[str]] = None
        self.selection_filter_paths: Optional[Set[str]] = None
        self.similarity_filter_paths: Optional[Set[str]] = None
        self.duplicates_only: bool = False
        self.current_date_filter: Optional[Tuple[float, float]] = None

        # Folder navigation state
        self.folder_nodes: Dict[str, FolderNode] = {}
        self.navigation_stack: List[str] = []

        # RAW+JPG group state
        self.group_mode: bool = False
        self.group_map: Dict[str, FileGroup] = {}
        self.grouped_raw_paths: Set[str] = set()
        self.raw_to_primary: Dict[str, str] = {}

    # -- File addition -------------------------------------------------------

    def add_files(self, files: List[str]) -> Optional[AddBatchResult]:
        """Returns None if all files were already known."""
        new_files = [f for f in files if f not in self.all_files_set]
        if not new_files:
            return None

        start_idx = len(self.all_files)
        self.all_files.extend(new_files)
        self.all_files_set.update(new_files)
        for i, f in enumerate(new_files):
            orig_idx = start_idx + i
            self.path_to_idx[f] = orig_idx
            self.image_states[orig_idx] = ImageState()
            if f in self.initial_thumb_paths:
                self.thumb_path_cache[orig_idx] = self.initial_thumb_paths.pop(f)

        if self.group_mode:
            self._rebuild_group_map()

        return AddBatchResult(new_files=new_files, start_idx=start_idx)

    def append_to_visible(self, new_files: List[str]) -> None:
        """Append files to current_files and visible mappings (scan fast path)."""
        for f in new_files:
            vis_idx = len(self.current_files)
            orig_idx = self.path_to_idx[f]
            self.current_files.append(f)
            self.visible_to_original[vis_idx] = orig_idx
            self.original_to_visible[orig_idx] = vis_idx
            self.visible_original_indices.append(orig_idx)

    def insert_sorted(self, new_files: List[str]) -> None:
        """Precondition: new_files already in all_files and path_to_idx."""
        for f in sorted(new_files):
            orig_idx = self.path_to_idx[f]
            insert_pos = bisect.bisect_left(self.current_files, f)
            self.current_files.insert(insert_pos, f)
            self.visible_original_indices.insert(insert_pos, orig_idx)

        self._rebuild_visible_from_indices()

    def _rebuild_visible_from_indices(self) -> None:
        self.visible_to_original.clear()
        self.original_to_visible.clear()
        for vis_idx, orig_idx in enumerate(self.visible_original_indices):
            self.visible_to_original[vis_idx] = orig_idx
            self.original_to_visible[orig_idx] = vis_idx

    # -- File removal --------------------------------------------------------

    def remove_files(self, paths: List[str]) -> RemoveResult:
        paths_set = set(paths)

        # Capture which visible indices are being removed
        removed_vis_indices: Set[int] = set()
        for p in paths:
            orig = self.path_to_idx.get(p)
            if orig is not None:
                vis = self.original_to_visible.get(orig)
                if vis is not None:
                    removed_vis_indices.add(vis)

        # Snapshot old hidden paths before index shift
        old_hidden_paths = {
            self.all_files[i] for i in self.hidden_indices
            if i < len(self.all_files)
        }

        # Rebuild data arrays with new indices
        new_all_files: List[str] = []
        new_image_states: Dict[int, ImageState] = {}
        new_pixmap_cache: Dict[int, object] = {}
        new_thumb_path_cache: Dict[int, str] = {}
        current_new_idx = 0
        for original_idx, file_path in enumerate(self.all_files):
            if file_path not in paths_set:
                new_all_files.append(file_path)
                if original_idx in self.image_states:
                    new_image_states[current_new_idx] = self.image_states[original_idx]
                if original_idx in self.pixmap_cache:
                    new_pixmap_cache[current_new_idx] = self.pixmap_cache[original_idx]
                if original_idx in self.thumb_path_cache:
                    new_thumb_path_cache[current_new_idx] = self.thumb_path_cache[original_idx]
                current_new_idx += 1

        self.all_files = new_all_files
        self.all_files_set = set(new_all_files)
        self.path_to_idx = {path: idx for idx, path in enumerate(new_all_files)}
        self.image_states = new_image_states
        self.pixmap_cache = new_pixmap_cache
        self.thumb_path_cache = new_thumb_path_cache

        # Rebuild group map if active
        if self.group_mode:
            self._rebuild_group_map()

        # Shift _hidden_indices to match new original indices
        self.hidden_indices = set()
        for hp in old_hidden_paths:
            if self.group_mode and hp in self.raw_to_primary:
                continue
            new_idx = self.path_to_idx.get(hp)
            if new_idx is not None:
                self.hidden_indices.add(new_idx)
        if self.group_mode:
            for raw_path in self.grouped_raw_paths:
                idx = self.path_to_idx.get(raw_path)
                if idx is not None:
                    self.hidden_indices.add(idx)

        # Rebuild visible ↔ original mappings
        self.rebuild_visible_mappings()
        self.last_layout_file_count = len(self.all_files)

        return RemoveResult(
            removed_vis_indices=removed_vis_indices,
            removed_paths=paths_set,
        )

    # -- Reordering ----------------------------------------------------------

    def reorder(self, ordered_paths: List[str]) -> bool:
        """Returns True if the order actually changed. Unknown paths silently dropped."""
        old_idx_map = {path: idx for idx, path in enumerate(self.all_files)}
        new_all_files = [p for p in ordered_paths if p in old_idx_map]

        if not new_all_files or new_all_files == self.all_files:
            return False

        new_image_states: Dict[int, ImageState] = {}
        new_pixmap_cache: Dict[int, object] = {}
        new_thumb_path_cache: Dict[int, str] = {}
        for new_idx, path in enumerate(new_all_files):
            old_idx = old_idx_map[path]
            if old_idx in self.image_states:
                new_image_states[new_idx] = self.image_states[old_idx]
            if old_idx in self.pixmap_cache:
                new_pixmap_cache[new_idx] = self.pixmap_cache[old_idx]
            if old_idx in self.thumb_path_cache:
                new_thumb_path_cache[new_idx] = self.thumb_path_cache[old_idx]

        self.all_files = new_all_files
        self.all_files_set = set(new_all_files)
        self.path_to_idx = {path: idx for idx, path in enumerate(new_all_files)}
        self.image_states = new_image_states
        self.pixmap_cache = new_pixmap_cache
        self.thumb_path_cache = new_thumb_path_cache
        return True

    # -- Visible mapping management ------------------------------------------

    def rebuild_visible_mappings(self) -> None:
        self.current_files = []
        self.visible_to_original.clear()
        self.original_to_visible.clear()
        self.visible_original_indices.clear()

        visible_idx = 0
        for original_idx, file_path in enumerate(self.all_files):
            if original_idx not in self.hidden_indices:
                self.current_files.append(file_path)
                self.visible_to_original[visible_idx] = original_idx
                self.original_to_visible[original_idx] = visible_idx
                self.visible_original_indices.append(original_idx)
                visible_idx += 1

    # -- Filter management ---------------------------------------------------

    def compute_hidden_indices(self, visible_paths: Set[str]) -> Tuple[Set[int], bool]:
        """Returns (new_hidden_indices, changed)."""
        # Folders are always visible regardless of filters
        if self.folder_nodes:
            visible_paths = visible_paths | self.folder_nodes.keys()
        new_hidden: Set[int] = set()
        for i, file_path in enumerate(self.all_files):
            if file_path not in visible_paths:
                new_hidden.add(i)
            elif self.group_mode and file_path in self.grouped_raw_paths:
                new_hidden.add(i)

        hidden_changed = self.hidden_indices != new_hidden
        count_changed = len(self.all_files) != self.last_layout_file_count
        return new_hidden, hidden_changed or count_changed

    def apply_hidden_indices(self, new_hidden: Set[int]) -> None:
        self.hidden_indices = new_hidden
        self.rebuild_visible_mappings()
        self.last_layout_file_count = len(self.all_files)

    def set_text_filter(self, text: str) -> None:
        self.current_filter = text

    def set_star_filter(self, states: list) -> None:
        self.current_star_filter = states

    def set_tag_filter(self, tags: list) -> None:
        self.current_tag_filter = list(tags)

    def set_clip_search_paths(self, paths: Optional[Set[str]]) -> None:
        self.clip_search_paths = paths

    def set_person_filter_paths(self, paths: Optional[Set[str]]) -> None:
        self.person_filter_paths = paths

    def set_selection_filter_paths(self, paths: Optional[Set[str]]) -> None:
        self.selection_filter_paths = paths

    def set_similarity_filter_paths(self, paths: Optional[Set[str]]) -> None:
        self.similarity_filter_paths = paths

    def set_duplicates_only(self, enabled: bool) -> None:
        self.duplicates_only = enabled

    def set_date_filter(self, date_range: Optional[Tuple[float, float]]) -> None:
        self.current_date_filter = date_range

    def clear_filters(self) -> None:
        """Reset all filter state."""
        self.current_filter = ""
        self.current_star_filter = [True, True, True, True, True, True]
        self.current_tag_filter = []
        self.current_date_filter = None
        self.clip_search_paths = None
        self.person_filter_paths = None
        self.selection_filter_paths = None
        self.similarity_filter_paths = None
        self.duplicates_only = False
        self.hidden_indices = set()

    def filter_affects_rating(self) -> bool:
        return not all(self.current_star_filter)

    def has_active_tag_filter(self) -> bool:
        return bool(self.current_tag_filter)

    # -- Cache management ----------------------------------------------------

    def invalidate(self, paths: List[str]) -> List[int]:
        """Returns affected original indices."""
        affected: List[int] = []
        for path in paths:
            idx = self.path_to_idx.get(path)
            if idx is None:
                continue
            state = self.image_states.get(idx)
            if state:
                state.loaded = False
                state.prioritized = False
            self.pixmap_cache.pop(idx, None)
            self.thumb_path_cache.pop(idx, None)
            affected.append(idx)
        return affected

    # -- RAW+JPG grouping ---------------------------------------------------

    def _rebuild_group_map(self) -> None:
        self.group_map, self.grouped_raw_paths = build_group_map(self.all_files)
        self.raw_to_primary = {}
        for primary, group in self.group_map.items():
            for raw in group.raw_files:
                self.raw_to_primary[raw] = primary

    def toggle_group_mode(self) -> str:
        """Returns a status message."""
        self.group_mode = not self.group_mode
        if self.group_mode:
            self._rebuild_group_map()
        else:
            self.group_map.clear()
            self.grouped_raw_paths.clear()
            self.raw_to_primary.clear()
        state = "ON" if self.group_mode else "OFF"
        hidden = len(self.grouped_raw_paths) if self.group_mode else 0
        msg = f"RAW+JPG grouping: {state}"
        if hidden:
            msg += f" ({hidden} RAW files grouped)"
        return msg

    def get_group_for_path(self, path: str) -> Optional[FileGroup]:
        group = self.group_map.get(path)
        if group:
            return group
        primary = self.raw_to_primary.get(path)
        if primary:
            return self.group_map.get(primary)
        return None

    # -- Reset ---------------------------------------------------------------

    def clear(self) -> None:
        """Reset data structures for a new directory.  Preserves filter/group state."""
        self.all_files.clear()
        self.all_files_set.clear()
        self.current_files.clear()
        self.path_to_idx.clear()
        self.visible_to_original.clear()
        self.original_to_visible.clear()
        self.visible_original_indices.clear()
        self.hidden_indices.clear()
        self.image_states.clear()
        self.pixmap_cache.clear()
        self.thumb_path_cache.clear()
        self.initial_thumb_paths.clear()
        self.current_directory_path = None
        self.folder_nodes.clear()
        self.navigation_stack.clear()
        self.scan_active = False
        self.last_layout_file_count = 0
