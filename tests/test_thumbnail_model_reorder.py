"""Tests for ThumbnailModel.reorder() under active filters."""
from gui.thumbnail_model import ThumbnailModel


def _model_with_files(paths):
    m = ThumbnailModel()
    m.add_files(paths)
    m.rebuild_visible_mappings()
    return m


def _apply_filter(model, visible_paths):
    new_hidden, _ = model.compute_hidden_indices(set(visible_paths))
    model.apply_hidden_indices(new_hidden)


class TestReorderNoFilter:
    def test_reorders_all_files(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        assert m.reorder(["/c.jpg", "/a.jpg", "/b.jpg"])
        assert m.all_files == ["/c.jpg", "/a.jpg", "/b.jpg"]
        assert m.current_files == ["/c.jpg", "/a.jpg", "/b.jpg"]

    def test_no_change_returns_false(self):
        m = _model_with_files(["/a.jpg", "/b.jpg"])
        assert not m.reorder(["/a.jpg", "/b.jpg"])

    def test_unknown_paths_dropped_gracefully(self):
        m = _model_with_files(["/a.jpg", "/b.jpg"])
        assert m.reorder(["/b.jpg", "/a.jpg", "/unknown.jpg"])
        assert m.all_files == ["/b.jpg", "/a.jpg"]

    def test_duplicate_paths_not_corrupt(self):
        """Duplicate entries in ordered_paths must not corrupt all_files."""
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        m.reorder(["/c.jpg", "/a.jpg", "/c.jpg"])  # /c.jpg appears twice
        assert len(m.all_files) == 3
        assert set(m.all_files) == {"/a.jpg", "/b.jpg", "/c.jpg"}

    def test_path_to_idx_updated(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        m.reorder(["/c.jpg", "/a.jpg", "/b.jpg"])
        assert m.path_to_idx["/c.jpg"] == 0
        assert m.path_to_idx["/a.jpg"] == 1
        assert m.path_to_idx["/b.jpg"] == 2

    def test_all_files_set_consistent(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        m.reorder(["/c.jpg", "/a.jpg", "/b.jpg"])
        assert m.all_files_set == set(m.all_files)


class TestReorderUnderFilter:
    def test_hidden_files_preserved_in_all_files(self):
        """Hidden files must not be dropped when sorting the visible subset."""
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])  # b, d hidden
        assert m.current_files == ["/a.jpg", "/c.jpg", "/e.jpg"]

        # Sort visible subset: e, a, c
        assert m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        # all_files must contain all 5 files
        assert set(m.all_files) == {"/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"}
        assert len(m.all_files) == 5

    def test_visible_files_sorted_correctly(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])

        m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        assert m.current_files == ["/e.jpg", "/a.jpg", "/c.jpg"]

    def test_hidden_files_still_hidden_after_reorder(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])

        m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        hidden_paths = {m.all_files[i] for i in m.hidden_indices}
        assert hidden_paths == {"/b.jpg", "/d.jpg"}

    def test_clearing_filter_restores_all_files(self):
        """After sorting under a filter, clearing the filter must restore all files."""
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])
        m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        # Clear filter
        m.apply_hidden_indices(set())

        assert len(m.current_files) == 5
        assert set(m.current_files) == {"/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"}

    def test_visible_to_original_mapping_correct(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])
        m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        # Visible indices 0,1,2 must map to the sorted visible files
        resolved = [m.all_files[m.visible_to_original[i]] for i in range(3)]
        assert resolved == ["/e.jpg", "/a.jpg", "/c.jpg"]

    def test_hidden_files_interleaved_at_original_positions(self):
        """Hidden files stay at their original index slots in all_files."""
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"])
        # b(idx=1) and d(idx=3) are hidden
        _apply_filter(m, ["/a.jpg", "/c.jpg", "/e.jpg"])

        m.reorder(["/e.jpg", "/a.jpg", "/c.jpg"])

        # Slots 1 and 3 must still hold the hidden files
        hidden_slot_1 = m.all_files[1]
        hidden_slot_3 = m.all_files[3]
        assert hidden_slot_1 == "/b.jpg"
        assert hidden_slot_3 == "/d.jpg"

    def test_reorder_idempotent_on_already_sorted(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg"])
        # Already in the right order
        assert not m.reorder(["/a.jpg", "/c.jpg"])

    def test_image_states_remapped_correctly(self):
        m = _model_with_files(["/a.jpg", "/b.jpg", "/c.jpg"])
        _apply_filter(m, ["/a.jpg", "/c.jpg"])
        # Mark /c.jpg as loaded
        m.image_states[m.path_to_idx["/c.jpg"]].loaded = True

        m.reorder(["/c.jpg", "/a.jpg"])

        new_idx_c = m.path_to_idx["/c.jpg"]
        assert m.image_states[new_idx_c].loaded is True

    def test_empty_ordered_paths_no_change(self):
        m = _model_with_files(["/a.jpg", "/b.jpg"])
        _apply_filter(m, ["/a.jpg"])
        assert not m.reorder([])

    def test_all_files_hidden_no_change(self):
        m = _model_with_files(["/a.jpg", "/b.jpg"])
        _apply_filter(m, [])  # all hidden
        assert not m.reorder(["/a.jpg", "/b.jpg"])
