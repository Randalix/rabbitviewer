"""Heatmap ring-distance priority computation.

Pure-function module — no Qt or daemon dependencies.
Thumb zone: 10-ring Manhattan diamond.  Fullres zone: 4-ring diamond offset
by FULLRES_OFFSET so the first few thumb rings render before fullres begins.
"""

from typing import List, Set, Tuple

THUMB_RING_COUNT = 10
FULLRES_RING_COUNT = 4
BASE = 90
STEP = 5
FULLRES_OFFSET = 3


def heatmap_priority(ring: int, is_fullres: bool = False) -> int:
    """``BASE - ring * STEP``, shifted by FULLRES_OFFSET for fullres."""
    if is_fullres:
        return BASE - (ring + FULLRES_OFFSET) * STEP
    return BASE - ring * STEP


def ring_distance(row: int, col: int, center_row: int, center_col: int) -> int:
    return abs(row - center_row) + abs(col - center_col)


def compute_heatmap(
    center_row: int,
    center_col: int,
    columns: int,
    total_visible: int,
    loaded_set: Set[int],
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Return ``(thumb_pairs, fullres_pairs)`` as ``[(visible_idx, priority), ...]``.

    *thumb_pairs* excludes indices in *loaded_set*; *fullres_pairs* does not.
    Both lists are sorted by priority descending.
    """
    if columns <= 0 or total_visible <= 0:
        return [], []

    max_ring = max(THUMB_RING_COUNT, FULLRES_RING_COUNT)
    total_rows = (total_visible + columns - 1) // columns

    min_row = max(0, center_row - max_ring)
    max_row = min(total_rows - 1, center_row + max_ring)
    min_col = max(0, center_col - max_ring)
    max_col = min(columns - 1, center_col + max_ring)

    thumb_pairs: List[Tuple[int, int]] = []
    fullres_pairs: List[Tuple[int, int]] = []

    for r in range(min_row, max_row + 1):
        for c in range(min_col, max_col + 1):
            vis_idx = r * columns + c
            if vis_idx >= total_visible:
                continue

            dist = abs(r - center_row) + abs(c - center_col)

            if dist <= THUMB_RING_COUNT and vis_idx not in loaded_set:
                thumb_pairs.append((vis_idx, BASE - dist * STEP))

            if dist <= FULLRES_RING_COUNT:
                fullres_pairs.append((vis_idx, BASE - (dist + FULLRES_OFFSET) * STEP))

    thumb_pairs.sort(key=lambda t: -t[1])
    fullres_pairs.sort(key=lambda t: -t[1])

    return thumb_pairs, fullres_pairs
