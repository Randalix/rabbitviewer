"""Tests for PictureBase orientation support."""

import pytest
from unittest.mock import MagicMock
from gui.picture_base import PictureBase


def _make_image(w=200, h=100):
    img = MagicMock()
    img.isNull.return_value = False
    img.width.return_value = w
    img.height.return_value = h
    # transformed() returns a rotated mock with swapped dimensions
    rotated = MagicMock()
    rotated.isNull.return_value = False
    rotated.width.return_value = h
    rotated.height.return_value = w
    img.transformed.return_value = rotated
    return img


@pytest.fixture
def pb():
    """Create a PictureBase with a mock image loaded."""
    pb = PictureBase()
    pb.setImage(_make_image())
    return pb


class TestSetImage:
    def test_resets_orientation(self, pb):
        pb.setOrientation(90)
        assert pb.orientation() == 90

        pb.setImage(_make_image(300, 200))
        assert pb.orientation() == 0

    def test_stores_raw_image(self, pb):
        assert pb._raw_image is not None
        assert pb._raw_image is pb._image  # no rotation → same object


class TestSetOrientation:
    def test_basic(self, pb):
        pb.setOrientation(90)
        assert pb.orientation() == 90

    def test_wraps_360(self, pb):
        pb.setOrientation(450)
        assert pb.orientation() == 90

    def test_noop_same_degrees(self, pb):
        pb.setOrientation(90)
        signals = []
        pb.viewStateChanged.connect(lambda vs: signals.append(vs))
        pb.setOrientation(90)
        assert len(signals) == 0

    def test_marks_transform_dirty(self, pb):
        pb._transform_dirty = False
        pb.setOrientation(180)
        assert pb._transform_dirty is True

    def test_zero_keeps_raw(self, pb):
        pb.setOrientation(90)
        pb.setOrientation(0)
        assert pb._image is pb._raw_image

    def test_nonzero_calls_transformed(self, pb):
        pb.setOrientation(90)
        pb._raw_image.transformed.assert_called_once()
        assert pb._image is pb._raw_image.transformed.return_value

    def test_preserves_raw(self, pb):
        original_raw = pb._raw_image
        pb.setOrientation(90)
        pb.setOrientation(180)
        assert pb._raw_image is original_raw


class TestSetOrientationFromExif:
    @pytest.mark.parametrize("exif,expected_degrees", [
        (1, 0), (3, 180), (6, 90), (8, 270),
    ])
    def test_known_values(self, pb, exif, expected_degrees):
        pb.setOrientationFromExif(exif)
        assert pb.orientation() == expected_degrees

    def test_unknown_value_defaults_zero(self, pb):
        pb.setOrientationFromExif(2)
        assert pb.orientation() == 0

    def test_unknown_value_defaults_zero_for_5(self, pb):
        pb.setOrientationFromExif(5)
        assert pb.orientation() == 0


class TestRotateBy:
    def test_cumulative(self, pb):
        pb.setOrientation(90)
        pb.rotateBy(90)
        assert pb.orientation() == 180

    def test_wraps(self, pb):
        pb.setOrientation(270)
        pb.rotateBy(180)
        assert pb.orientation() == 90


class TestExifOrientationDegreesConstant:
    def test_accessible_from_class(self):
        assert PictureBase._EXIF_ORIENTATION_DEGREES == {1: 0, 3: 180, 6: 90, 8: 270}
