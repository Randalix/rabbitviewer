from __future__ import annotations
import time
import logging
from typing import Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from core.heatmap import compute_heatmap, THUMB_RING_COUNT
from core.priority import Priority

if TYPE_CHECKING:
    from gui.thumbnail_model import ThumbnailModel

logger = logging.getLogger(__name__)


class ViewportPrioritizer:

    _PREVIEW_TICK_BATCH = 20
    _STALE_THRESHOLD_S = 5.0
    _FULLRES_DEBOUNCE_S = 0.4
    # Number of folder images added to the pre-warm queue per heatmap send window.
    # Progressive: one batch is submitted every _FULLRES_DEBOUNCE_S seconds until
    # the folder is fully warmed or leaves the fullres range.
    _FOLDER_FULLRES_BATCH = 5

    def __init__(self, model: ThumbnailModel, send_heatmap: Callable):
        self.model = model
        self._send_heatmap = send_heatmap

        self._last_thumb_pairs: Dict[str, int] = {}
        self._last_fullres_pairs: Dict[str, int] = {}
        self._stale_request_ts: Dict[str, float] = {}
        self._last_fullres_send_time: float = 0.0
        # Per-folder warmup cursor: folder_path → next image index to submit.
        # Reset to zero when the folder card leaves the fullres heatmap range.
        self._folder_warmup_cursors: Dict[str, int] = {}

        self._pending_previews: List[Tuple[str, str]] = []

    # -- Core priority computation -------------------------------------------

    def compute_and_send(
        self,
        hovered_orig_idx: Optional[int],
        visible_rows: Tuple[int, int],
        columns: int,
        total_visible: int,
        is_loading: bool,
    ) -> None:
        """Compute heatmap priorities and send deltas to the daemon.

        *visible_rows* is (first_row, last_row) from VirtualGridManager.
        """
        if columns <= 0 or total_visible == 0:
            return

        model = self.model

        # --- Determine heatmap center ---
        ref_visible_idx = None
        if hovered_orig_idx is not None:
            ref_visible_idx = model.original_to_visible.get(hovered_orig_idx)

        if ref_visible_idx is None:
            first_row, last_row = visible_rows
            first_row = max(0, first_row - 1)
            last_row += 1
            start_idx = first_row * columns
            end_idx = min(total_visible - 1, (last_row + 1) * columns - 1)
            ref_visible_idx = (start_idx + end_idx) // 2

        center_row, center_col = divmod(ref_visible_idx, columns)

        # --- Build loaded_set scoped to the heatmap bounding box ---
        total_rows = (total_visible + columns - 1) // columns
        bb_min_row = max(0, center_row - THUMB_RING_COUNT)
        bb_max_row = min(total_rows - 1, center_row + THUMB_RING_COUNT)
        bb_min_col = max(0, center_col - THUMB_RING_COUNT)
        bb_max_col = min(columns - 1, center_col + THUMB_RING_COUNT)

        loaded_set: Set[int] = set()
        for r in range(bb_min_row, bb_max_row + 1):
            for c in range(bb_min_col, bb_max_col + 1):
                vis_idx = r * columns + c
                if vis_idx >= total_visible:
                    continue
                orig_idx = model.visible_to_original.get(vis_idx)
                if orig_idx is not None:
                    state = model.image_states.get(orig_idx)
                    if state and state.loaded:
                        loaded_set.add(vis_idx)

        # --- Compute heatmap ---
        thumb_pairs, fullres_pairs = compute_heatmap(
            center_row, center_col, columns,
            total_visible, loaded_set,
        )

        # --- Map visible_idx → file path, build {path: priority} dicts ---
        vis_to_orig = model.visible_to_original.get
        all_files = model.all_files
        folder_paths = model.folder_nodes

        current_thumb: Dict[str, int] = {}
        for vis_idx, priority in thumb_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                path = all_files[orig_idx]
                if path not in folder_paths:
                    current_thumb[path] = priority

        # --- Cached/post-scan: request full viewport at GUI_REQUEST_LOW ---
        if not is_loading:
            first_row, last_row = visible_rows
            first_row = max(0, first_row - 1)
            last_row += 1
            vis_start = first_row * columns
            vis_end = min(total_visible - 1, (last_row + 1) * columns - 1)
            for vis_idx in range(vis_start, vis_end + 1):
                orig_idx = vis_to_orig(vis_idx)
                if orig_idx is None:
                    continue
                state = model.image_states.get(orig_idx)
                if state and state.loaded:
                    continue
                path = all_files[orig_idx]
                if path not in current_thumb and path not in folder_paths:
                    current_thumb[path] = int(Priority.GUI_REQUEST_LOW)

        # Compute debounce state once so the folder cursor and the send gate
        # both use the same timestamp.
        now = time.monotonic()
        fullres_debounced = now - self._last_fullres_send_time < self._FULLRES_DEBOUNCE_S

        active_folder_cards: Set[str] = set()
        current_fullres: Dict[str, int] = {}
        for vis_idx, priority in fullres_pairs:
            orig_idx = vis_to_orig(vis_idx)
            if orig_idx is not None:
                path = all_files[orig_idx]
                node = folder_paths.get(path)
                if node is not None and node.image_paths:
                    active_folder_cards.add(path)
                    cursor = self._folder_warmup_cursors.get(path, 0)
                    # Advance cursor only when the send window is open so we
                    # submit exactly one batch per _FULLRES_DEBOUNCE_S interval.
                    if not fullres_debounced and cursor < len(node.image_paths):
                        cursor = min(cursor + self._FOLDER_FULLRES_BATCH, len(node.image_paths))
                        self._folder_warmup_cursors[path] = cursor
                    for i, img_path in enumerate(node.image_paths[:cursor]):
                        current_fullres[img_path] = self._folder_image_priority(
                            i, len(node.image_paths), priority
                        )
                else:
                    current_fullres[path] = priority

        # Reset warmup cursors for folder cards that left the fullres range.
        # Their images will naturally appear in fullres_to_cancel via the delta.
        for path in list(self._folder_warmup_cursors):
            if path not in active_folder_cards:
                del self._folder_warmup_cursors[path]

        # --- Early out: skip IPC when every (path, priority) pair is identical ---
        if current_thumb == self._last_thumb_pairs and current_fullres == self._last_fullres_pairs:
            now = time.monotonic()
            stale_evicted = 0
            for p in list(current_thumb):
                oi = model.path_to_idx.get(p, -1)
                if oi < 0:
                    continue
                st = model.image_states.get(oi)
                if st and not st.loaded:
                    first_seen = self._stale_request_ts.get(p)
                    if first_seen is None:
                        self._stale_request_ts[p] = now
                    elif now - first_seen >= self._STALE_THRESHOLD_S:
                        del self._last_thumb_pairs[p]
                        del self._stale_request_ts[p]
                        stale_evicted += 1
                else:
                    self._stale_request_ts.pop(p, None)
            if stale_evicted:
                logger.info("[heatmap] re-requesting %d stale thumbnails", stale_evicted)
            else:
                return

        # --- Compute deltas ---
        prev_thumb = self._last_thumb_pairs
        prev_fullres = self._last_fullres_pairs

        delta_upgrade = [(p, pri) for p, pri in current_thumb.items() if prev_thumb.get(p) != pri]
        paths_to_downgrade = [p for p in prev_thumb if p not in current_thumb]
        delta_fullres = [(p, pri) for p, pri in current_fullres.items() if prev_fullres.get(p) != pri]
        fullres_to_cancel = [p for p in prev_fullres if p not in current_fullres]

        # --- Debounce fullres to avoid schedule/cancel churn during rapid hover ---
        send_fullres = True
        if (delta_fullres or fullres_to_cancel) and fullres_debounced:
            delta_fullres = []
            fullres_to_cancel = []
            send_fullres = False

        # --- Send to daemon ---
        if delta_upgrade or paths_to_downgrade or delta_fullres or fullres_to_cancel:
            logger.debug(
                "[trace] heatmap IPC: upgrades=%d, downgrades=%d, fullres=%d, "
                "fullres_cancel=%d, is_loading=%s",
                len(delta_upgrade), len(paths_to_downgrade), len(delta_fullres),
                len(fullres_to_cancel), is_loading,
            )
            self._send_heatmap(delta_upgrade, paths_to_downgrade, delta_fullres, fullres_to_cancel)

        self._last_thumb_pairs = current_thumb
        if send_fullres:
            self._last_fullres_pairs = current_fullres
            self._last_fullres_send_time = now

    # -- Preview queue management --------------------------------------------

    def queue_previews(self, items: List[Tuple[str, str]]) -> None:
        self._pending_previews.extend(items)

    @property
    def has_pending_previews(self) -> bool:
        return bool(self._pending_previews)

    def drain_preview_batch(self) -> List[Tuple[str, str]]:
        """Sorted by heatmap priority so closest-to-cursor items load first."""
        if self._last_thumb_pairs and len(self._pending_previews) > self._PREVIEW_TICK_BATCH:
            pmap = self._last_thumb_pairs
            self._pending_previews.sort(key=lambda item: -pmap.get(item[0], 0))

        batch = self._pending_previews[:self._PREVIEW_TICK_BATCH]
        del self._pending_previews[:self._PREVIEW_TICK_BATCH]
        return batch

    def invalidate_thumb_pairs(self, path: str) -> None:
        self._last_thumb_pairs.pop(path, None)

    # -- Folder pre-warm -----------------------------------------------------

    def _folder_image_priority(self, image_idx: int, total_images: int, base_priority: int) -> int:
        """Return the render priority for the *image_idx*-th image in a folder.

        *image_idx*: 0-based position in the folder's sorted image list.
        *total_images*: total number of images in the folder.
        *base_priority*: heatmap priority of the folder card itself.

        Currently flat — every image gets *base_priority*.
        Replace with a position-aware strategy (e.g. evenly-spread importance
        sampling across the full range) when that feature is introduced.
        """
        return base_priority

    # -- Lifecycle -----------------------------------------------------------

    def clear(self) -> List[str]:
        """Returns fullres paths to cancel."""
        cancel_paths = list(self._last_fullres_pairs.keys()) if self._last_fullres_pairs else []
        self._last_thumb_pairs = {}
        self._last_fullres_pairs = {}
        self._stale_request_ts = {}
        self._last_fullres_send_time = 0.0
        self._folder_warmup_cursors.clear()
        self._pending_previews.clear()
        return cancel_paths
