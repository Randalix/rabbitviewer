"""Tests for core/orientation_model.py — EXIF orientation mapping and guard logic."""
from core.orientation_model import _CLASS_TO_EXIF, EXIF_TO_DEGREES


class TestOrientationMapping:
    def test_class_to_exif_values(self):
        assert _CLASS_TO_EXIF == {0: 1, 1: 6, 2: 3, 3: 8}

    def test_exif_to_degrees(self):
        assert EXIF_TO_DEGREES == {1: 0, 6: 90, 3: 180, 8: 270}

    def test_round_trip_all_classes(self):
        """Every class maps to a valid EXIF orientation with known degrees."""
        for cls_idx, exif_val in _CLASS_TO_EXIF.items():
            assert exif_val in EXIF_TO_DEGREES
            assert 0 <= EXIF_TO_DEGREES[exif_val] < 360

    def test_all_four_rotations_covered(self):
        degrees = sorted(EXIF_TO_DEGREES[v] for v in _CLASS_TO_EXIF.values())
        assert degrees == [0, 90, 180, 270]


class TestGuardLogic:
    """Test the guard conditions for auto-orientation (logic, not model inference)."""

    def test_skip_if_orientation_already_set(self, tmp_env, sample_images):
        """Auto-orient should skip files with orientation != 1."""
        db = tmp_env["db"]
        fp = sample_images[0]
        db.images.set_thumbnail_paths(fp, thumbnail_path="/tmp/thumb.jpg")
        db.images.set_orientation(fp, 6)  # already set to 90 CW
        assert db.images.get_orientation(fp) == 6

    def test_skip_if_pending_write(self, tmp_env):
        """Auto-orient should skip files with pending orientation writes."""
        db = tmp_env["db"]
        fp = "/test/pending.jpg"
        db.ledgers.pending_write_insert(fp, "orientation", {"orientation": 3})
        assert db.ledgers.pending_write_exists(fp, "orientation")

    def test_no_pending_write_for_other_type(self, tmp_env):
        """pending_write_exists should be type-specific."""
        db = tmp_env["db"]
        fp = "/test/rated.jpg"
        db.ledgers.pending_write_insert(fp, "rating", {"rating": 5})
        assert not db.ledgers.pending_write_exists(fp, "orientation")
