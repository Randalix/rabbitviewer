"""Tests for sort scripts (scripts/sort_by_*.py)."""
from unittest.mock import MagicMock

from scripts.sort_by_date import run_script as sort_by_date
from scripts.sort_by_name import run_script as sort_by_name
from scripts.sort_by_rating import run_script as sort_by_rating
from scripts.sort_by_size import run_script as sort_by_size
from scripts.sort_by_type import run_script as sort_by_type


def _make_api(paths, metadata=None):
    api = MagicMock()
    api.get_all_images.return_value = list(paths)
    api.get_metadata_batch.return_value = metadata or {}
    return api


# ---------------------------------------------------------------------------
# sort_by_date
# ---------------------------------------------------------------------------

class TestSortByDate:
    def test_sorts_by_date_taken(self):
        paths = ["/b.jpg", "/a.jpg", "/c.jpg"]
        metadata = {
            "/b.jpg": {"date_taken": 2000.0, "mtime": 1.0},
            "/a.jpg": {"date_taken": 1000.0, "mtime": 2.0},
            "/c.jpg": {"date_taken": 3000.0, "mtime": 3.0},
        }
        api = _make_api(paths, metadata)
        sort_by_date(api)
        api.set_image_order.assert_called_once_with(["/a.jpg", "/b.jpg", "/c.jpg"])

    def test_falls_back_to_mtime(self):
        paths = ["/b.jpg", "/a.jpg"]
        metadata = {
            "/b.jpg": {"date_taken": None, "mtime": 200.0},
            "/a.jpg": {"date_taken": None, "mtime": 100.0},
        }
        api = _make_api(paths, metadata)
        sort_by_date(api)
        api.set_image_order.assert_called_once_with(["/a.jpg", "/b.jpg"])

    def test_mixed_date_taken_and_mtime(self):
        """date_taken (float) and mtime (float) must be comparable."""
        paths = ["/b.jpg", "/a.jpg"]
        metadata = {
            "/b.jpg": {"date_taken": 5000.0, "mtime": 1.0},
            "/a.jpg": {"date_taken": None, "mtime": 3000.0},
        }
        api = _make_api(paths, metadata)
        sort_by_date(api)
        api.set_image_order.assert_called_once_with(["/a.jpg", "/b.jpg"])

    def test_already_sorted_no_call(self):
        paths = ["/a.jpg", "/b.jpg"]
        metadata = {
            "/a.jpg": {"date_taken": 1000.0},
            "/b.jpg": {"date_taken": 2000.0},
        }
        api = _make_api(paths, metadata)
        sort_by_date(api)
        api.set_image_order.assert_not_called()

    def test_empty_paths(self):
        api = _make_api([])
        sort_by_date(api)
        api.set_image_order.assert_not_called()

    def test_no_metadata(self):
        """When daemon returns no metadata, falls back to os.path.getmtime."""
        paths = ["/b.jpg", "/a.jpg"]
        api = _make_api(paths, {})
        sort_by_date(api)
        # Should not crash; order depends on filesystem, just verify no exception


# ---------------------------------------------------------------------------
# sort_by_name
# ---------------------------------------------------------------------------

class TestSortByName:
    def test_sorts_by_filename(self):
        paths = ["/dir/charlie.jpg", "/dir/alpha.jpg", "/dir/bravo.jpg"]
        api = _make_api(paths)
        sort_by_name(api)
        api.set_image_order.assert_called_once_with(
            ["/dir/alpha.jpg", "/dir/bravo.jpg", "/dir/charlie.jpg"]
        )

    def test_case_insensitive(self):
        paths = ["/dir/Bravo.jpg", "/dir/alpha.jpg"]
        api = _make_api(paths)
        sort_by_name(api)
        api.set_image_order.assert_called_once_with(
            ["/dir/alpha.jpg", "/dir/Bravo.jpg"]
        )


# ---------------------------------------------------------------------------
# sort_by_rating
# ---------------------------------------------------------------------------

class TestSortByRating:
    def test_sorts_descending(self):
        paths = ["/a.jpg", "/b.jpg", "/c.jpg"]
        metadata = {
            "/a.jpg": {"rating": 1},
            "/b.jpg": {"rating": 5},
            "/c.jpg": {"rating": 3},
        }
        api = _make_api(paths, metadata)
        sort_by_rating(api)
        api.set_image_order.assert_called_once_with(["/b.jpg", "/c.jpg", "/a.jpg"])

    def test_none_rating_treated_as_zero(self):
        paths = ["/a.jpg", "/b.jpg"]
        metadata = {
            "/a.jpg": {"rating": None},
            "/b.jpg": {"rating": 3},
        }
        api = _make_api(paths, metadata)
        sort_by_rating(api)
        api.set_image_order.assert_called_once_with(["/b.jpg", "/a.jpg"])


# ---------------------------------------------------------------------------
# sort_by_size
# ---------------------------------------------------------------------------

class TestSortBySize:
    def test_sorts_ascending(self):
        paths = ["/big.jpg", "/small.jpg"]
        metadata = {
            "/big.jpg": {"file_size": 5000},
            "/small.jpg": {"file_size": 100},
        }
        api = _make_api(paths, metadata)
        sort_by_size(api)
        api.set_image_order.assert_called_once_with(["/small.jpg", "/big.jpg"])


# ---------------------------------------------------------------------------
# sort_by_type
# ---------------------------------------------------------------------------

class TestSortByType:
    def test_sorts_by_extension_then_name(self):
        paths = ["/z.png", "/a.jpg", "/b.png", "/c.jpg"]
        api = _make_api(paths)
        sort_by_type(api)
        api.set_image_order.assert_called_once_with(
            ["/a.jpg", "/c.jpg", "/b.png", "/z.png"]
        )
