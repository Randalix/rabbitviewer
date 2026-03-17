"""Tests for CLIP embedding storage in MetadataDatabase."""
import struct
import pytest

from core.metadata_database import MetadataDatabase


def _fake_embedding(dim=512, val=0.1):
    """Create a fake float32 embedding blob."""
    return struct.pack(f'{dim}f', *([val] * dim))


class TestEmbeddingCRUD:
    def test_upsert_and_get(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        fp = "/test/image.jpg"
        blob = _fake_embedding()

        assert db.embeddings.upsert_embedding(fp, blob)
        result = db.embeddings.get_embedding(fp)
        assert result == blob

    def test_upsert_overwrites(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        fp = "/test/image.jpg"
        blob1 = _fake_embedding(val=0.1)
        blob2 = _fake_embedding(val=0.9)

        db.embeddings.upsert_embedding(fp, blob1)
        db.embeddings.upsert_embedding(fp, blob2)
        result = db.embeddings.get_embedding(fp)
        assert result == blob2

    def test_get_nonexistent(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.embeddings.get_embedding("/no/such/file.jpg") is None

    def test_blob_size_512_dim(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        fp = "/test/image.jpg"
        blob = _fake_embedding(dim=512)
        assert len(blob) == 512 * 4  # 2048 bytes

        db.embeddings.upsert_embedding(fp, blob)
        result = db.embeddings.get_embedding(fp)
        assert len(result) == 2048

    def test_get_all_embeddings(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        for i in range(5):
            db.embeddings.upsert_embedding(f"/test/img_{i}.jpg", _fake_embedding(val=float(i)))
        rows = db.embeddings.get_all_embeddings()
        assert len(rows) == 5
        paths = {r[0] for r in rows}
        assert paths == {f"/test/img_{i}.jpg" for i in range(5)}

    def test_get_all_filters_by_model(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.embeddings.upsert_embedding("/a.jpg", _fake_embedding(), model_name="clip-vit-b-32")
        db.embeddings.upsert_embedding("/b.jpg", _fake_embedding(), model_name="other-model")
        rows = db.embeddings.get_all_embeddings(model_name="clip-vit-b-32")
        assert len(rows) == 1
        assert rows[0][0] == "/a.jpg"

    def test_count_embeddings(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.embeddings.count_embeddings() == 0
        for i in range(3):
            db.embeddings.upsert_embedding(f"/test/img_{i}.jpg", _fake_embedding())
        assert db.embeddings.count_embeddings() == 3


class TestMissingEmbeddings:
    def test_all_missing(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        paths = ["/a.jpg", "/b.jpg", "/c.jpg"]
        missing = db.embeddings.get_files_missing_embeddings(paths)
        assert set(missing) == set(paths)

    def test_some_present(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.embeddings.upsert_embedding("/a.jpg", _fake_embedding())
        db.embeddings.upsert_embedding("/b.jpg", _fake_embedding())
        missing = db.embeddings.get_files_missing_embeddings(["/a.jpg", "/b.jpg", "/c.jpg"])
        assert missing == ["/c.jpg"]

    def test_empty_input(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.embeddings.get_files_missing_embeddings([]) == []


class TestCascadeDelete:
    def test_remove_records_deletes_embeddings(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        fp = "/test/image.jpg"

        # Create a metadata record first so remove_records has something to delete
        db.images.set_thumbnail_paths(fp, thumbnail_path="/tmp/thumb.jpg")
        db.embeddings.upsert_embedding(fp, _fake_embedding())
        assert db.embeddings.get_embedding(fp) is not None

        db.remove_records([fp])
        assert db.embeddings.get_embedding(fp) is None


class TestPendingWriteExists:
    def test_no_pending(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert not db.ledgers.pending_write_exists("/a.jpg", "orientation")

    def test_with_pending(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.ledgers.pending_write_insert("/a.jpg", "orientation", {"orientation": 6})
        assert db.ledgers.pending_write_exists("/a.jpg", "orientation")
        assert not db.ledgers.pending_write_exists("/a.jpg", "rating")
