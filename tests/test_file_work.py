"""Tests for the file_work work-intent ledger in MetadataDatabase."""

import pytest

from core.metadata_database import MetadataDatabase


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    return MetadataDatabase(db_path)


# -- batch insert ----------------------------------------------------------

class TestFileWorkBatchInsert:

    def test_inserts_rows(self, db):
        db.file_work_batch_insert(["/a.jpg", "/b.jpg"], "thumbnail", "/photos")
        result = db.file_work_get_pending("/photos", "thumbnail")
        assert sorted(result) == ["/a.jpg", "/b.jpg"]

    def test_idempotent(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        result = db.file_work_get_pending("/photos", "thumbnail")
        assert result == ["/a.jpg"]

    def test_empty_list_is_noop(self, db):
        db.file_work_batch_insert([], "thumbnail", "/photos")
        assert db.file_work_get_all_roots() == []

    def test_multiple_work_types(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/a.jpg"], "clip", "/photos")
        types = sorted(db.file_work_get_pending_types("/photos"))
        assert types == ["clip", "thumbnail"]


# -- remove ----------------------------------------------------------------

class TestFileWorkRemove:

    def test_removes_single_entry(self, db):
        db.file_work_batch_insert(["/a.jpg", "/b.jpg"], "thumbnail", "/photos")
        db.file_work_remove("/a.jpg", "thumbnail")
        assert db.file_work_get_pending("/photos", "thumbnail") == ["/b.jpg"]

    def test_remove_nonexistent_is_noop(self, db):
        db.file_work_remove("/nonexistent.jpg", "thumbnail")

    def test_remove_wrong_type_leaves_other(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_remove("/a.jpg", "clip")
        assert db.file_work_get_pending("/photos", "thumbnail") == ["/a.jpg"]


class TestFileWorkBatchRemove:

    def test_removes_multiple(self, db):
        db.file_work_batch_insert(["/a.jpg", "/b.jpg", "/c.jpg"], "clip", "/photos")
        db.file_work_batch_remove(["/a.jpg", "/c.jpg"], "clip")
        assert db.file_work_get_pending("/photos", "clip") == ["/b.jpg"]

    def test_empty_list_is_noop(self, db):
        db.file_work_batch_remove([], "clip")


# -- queries ---------------------------------------------------------------

class TestFileWorkQueries:

    def test_get_all_roots(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/b.jpg"], "clip", "/videos")
        roots = sorted(db.file_work_get_all_roots())
        assert roots == ["/photos", "/videos"]

    def test_get_all_roots_empty(self, db):
        assert db.file_work_get_all_roots() == []

    def test_get_pending_types(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/a.jpg"], "view_image", "/photos")
        db.file_work_batch_insert(["/a.jpg"], "clip", "/photos")
        types = sorted(db.file_work_get_pending_types("/photos"))
        assert types == ["clip", "thumbnail", "view_image"]

    def test_get_pending_types_empty(self, db):
        assert db.file_work_get_pending_types("/nonexistent") == []

    def test_get_pending_filters_by_root_and_type(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/b.jpg"], "thumbnail", "/videos")
        assert db.file_work_get_pending("/photos", "thumbnail") == ["/a.jpg"]
        assert db.file_work_get_pending("/videos", "thumbnail") == ["/b.jpg"]

    def test_same_file_two_roots(self, db):
        """A file discovered under two scan roots keeps both entries."""
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/backup")
        assert db.file_work_get_pending("/photos", "thumbnail") == ["/a.jpg"]
        assert db.file_work_get_pending("/backup", "thumbnail") == ["/a.jpg"]
        assert sorted(db.file_work_get_all_roots()) == ["/backup", "/photos"]


# -- clear all -------------------------------------------------------------

class TestFileWorkClearAll:

    def test_clears_everything(self, db):
        db.file_work_batch_insert(["/a.jpg"], "thumbnail", "/photos")
        db.file_work_batch_insert(["/b.jpg"], "clip", "/videos")
        cleared = db.file_work_clear_all()
        assert cleared == 2
        assert db.file_work_get_all_roots() == []

    def test_clear_empty_returns_zero(self, db):
        assert db.file_work_clear_all() == 0


# -- ledger_reset_all ------------------------------------------------------

class TestLedgerResetAll:

    def test_clears_all_ledger_entries(self, db):
        db.ledger_batch_insert(["/a.jpg", "/b.jpg"], scan_root="/photos")
        cleared = db.ledger_reset_all()
        assert cleared == 2
        assert db.ledger_get_all_scan_roots() == []

    def test_reset_empty_returns_zero(self, db):
        assert db.ledger_reset_all() == 0
