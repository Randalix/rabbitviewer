"""Tests for core.heatmap — ring distance and priority computation."""

import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from core.heatmap import (
    heatmap_priority, ring_distance, compute_heatmap,
    THUMB_RING_COUNT, FULLRES_RING_COUNT, BASE, STEP, FULLRES_OFFSET,
)


# ---------------------------------------------------------------------------
#  heatmap_priority
# ---------------------------------------------------------------------------

class TestHeatmapPriority:
    def test_thumb_ring_0(self):
        assert heatmap_priority(0) == 90

    def test_thumb_ring_10(self):
        assert heatmap_priority(10) == 60

    def test_fullres_ring_0(self):
        assert heatmap_priority(0, is_fullres=True) == 81

    def test_fullres_ring_4(self):
        assert heatmap_priority(4, is_fullres=True) == 69

    def test_thumb_monotonically_decreasing(self):
        priorities = [heatmap_priority(r) for r in range(THUMB_RING_COUNT + 1)]
        assert priorities == sorted(priorities, reverse=True)

    def test_fullres_monotonically_decreasing(self):
        priorities = [heatmap_priority(r, is_fullres=True) for r in range(FULLRES_RING_COUNT + 1)]
        assert priorities == sorted(priorities, reverse=True)

    def test_interleaving(self):
        """Fullres ring 0 priority == thumb ring FULLRES_OFFSET priority."""
        assert heatmap_priority(0, is_fullres=True) == heatmap_priority(FULLRES_OFFSET)

    def test_all_thumb_rings_hardcoded(self):
        expected = [90, 87, 84, 81, 78, 75, 72, 69, 66, 63, 60]
        actual = [heatmap_priority(r) for r in range(11)]
        assert actual == expected

    def test_all_fullres_rings_hardcoded(self):
        expected = [81, 78, 75, 72, 69]
        actual = [heatmap_priority(r, is_fullres=True) for r in range(5)]
        assert actual == expected


# ---------------------------------------------------------------------------
#  ring_distance
# ---------------------------------------------------------------------------

class TestRingDistance:
    def test_same_cell(self):
        assert ring_distance(3, 4, 3, 4) == 0

    def test_adjacent(self):
        assert ring_distance(3, 4, 3, 5) == 1
        assert ring_distance(3, 4, 4, 4) == 1

    def test_diagonal(self):
        assert ring_distance(0, 0, 1, 1) == 2

    def test_symmetry(self):
        assert ring_distance(2, 5, 7, 1) == ring_distance(7, 1, 2, 5)


# ---------------------------------------------------------------------------
#  compute_heatmap
# ---------------------------------------------------------------------------

class TestComputeHeatmap:
    def test_empty_grid(self):
        t, f = compute_heatmap(0, 0, 0, 0, set())
        assert t == [] and f == []

    def test_single_cell_unloaded(self):
        t, f = compute_heatmap(0, 0, 1, 1, set())
        assert t == [(0, 90)]
        assert f == [(0, 81)]

    def test_single_cell_loaded(self):
        """Loaded cells are excluded from thumb_pairs but included in fullres_pairs."""
        t, f = compute_heatmap(0, 0, 1, 1, {0})
        assert t == []
        assert f == [(0, 81)]

    def test_thumb_pairs_sorted_descending(self):
        t, _ = compute_heatmap(5, 5, 10, 100, set())
        priorities = [p for _, p in t]
        assert priorities == sorted(priorities, reverse=True)

    def test_fullres_pairs_sorted_descending(self):
        _, f = compute_heatmap(5, 5, 10, 100, set())
        priorities = [p for _, p in f]
        assert priorities == sorted(priorities, reverse=True)

    def test_center_gets_highest_priority(self):
        t, f = compute_heatmap(5, 5, 10, 100, set())
        center_idx = 5 * 10 + 5
        thumb_dict = dict(t)
        fullres_dict = dict(f)
        assert thumb_dict[center_idx] == 90
        assert fullres_dict[center_idx] == 81

    def test_ring_1_different_thumb_vs_fullres(self):
        """At distance 1, thumb and fullres give different priorities."""
        t, f = compute_heatmap(5, 5, 10, 100, set())
        # idx at ring 1: (5, 6) → vis_idx = 56
        thumb_dict = dict(t)
        fullres_dict = dict(f)
        assert thumb_dict[56] == 87   # 90 - 1*3
        assert fullres_dict[56] == 78  # 90 - (1+3)*3

    def test_no_oob_small_grid(self):
        """3x3 grid — no indices should exceed total_visible."""
        t, f = compute_heatmap(1, 1, 3, 9, set())
        for idx, _ in t:
            assert 0 <= idx < 9
        for idx, _ in f:
            assert 0 <= idx < 9

    def test_corner_center(self):
        """Heatmap centered at (0,0) in a 5-column grid."""
        t, f = compute_heatmap(0, 0, 5, 25, set())
        indices = {idx for idx, _ in t}
        # Ring 0: (0,0) = idx 0
        assert 0 in indices
        # Ring 1: (0,1)=1 and (1,0)=5
        assert 1 in indices
        assert 5 in indices

    def test_single_column_layout(self):
        """columns=1, total_visible=20, center at row 10."""
        t, f = compute_heatmap(10, 0, 1, 20, set())
        thumb_indices = {idx for idx, _ in t}
        # Ring 0 is idx 10, ring 1 is idx 9 and 11, etc.
        assert 10 in thumb_indices
        assert 9 in thumb_indices
        assert 11 in thumb_indices

    def test_loaded_set_filtering(self):
        """Items in loaded_set are excluded from thumb_pairs only."""
        loaded = {50, 51, 52}  # center area loaded
        t, f = compute_heatmap(5, 0, 10, 100, loaded)
        thumb_indices = {idx for idx, _ in t}
        fullres_indices = {idx for idx, _ in f}
        for idx in loaded:
            assert idx not in thumb_indices
        # Fullres includes loaded items within the 4-ring zone
        assert 50 in fullres_indices  # center cell (ring 0)
        assert 51 in fullres_indices  # ring 1 — loaded but still in fullres

    def test_fullres_zone_smaller_than_thumb_zone(self):
        """Fullres pairs should cover fewer cells than thumb pairs."""
        t, f = compute_heatmap(5, 5, 10, 100, set())
        assert len(f) <= len(t)

    def test_total_visible_clips_indices(self):
        """Grid partially filled: only valid indices appear."""
        # 3 columns, 7 items → row 2 has only 1 item (idx 6)
        t, f = compute_heatmap(1, 1, 3, 7, set())
        all_indices = {idx for idx, _ in t} | {idx for idx, _ in f}
        for idx in all_indices:
            assert idx < 7

    def test_negative_columns_returns_empty(self):
        t, f = compute_heatmap(0, 0, -1, 10, set())
        assert t == [] and f == []
