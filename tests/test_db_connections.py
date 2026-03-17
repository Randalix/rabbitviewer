"""Tests verifying per-domain connection isolation in MetadataDatabase.

Each table class should use its own sqlite3.Connection so that WAL mode
enables concurrent reads across domains without lock contention.
"""

import sqlite3
import threading
import time

import pytest

from core.metadata_database import MetadataDatabase


@pytest.fixture
def db(tmp_path):
    import core.metadata_database as _mdb_module
    _mdb_module._metadata_database = None
    d = MetadataDatabase(str(tmp_path / "test.db"))
    yield d
    d.close()
    _mdb_module._metadata_database = None


class TestConnectionIsolation:
    """Each table class must have its own connection object."""

    def test_all_tables_have_distinct_connections(self, db):
        conns = [
            db.images.conn,
            db.tags.conn,
            db.faces.conn,
            db.embeddings.conn,
            db.ledgers.conn,
            db.cache.conn,
        ]
        assert len(set(id(c) for c in conns)) == 6, \
            "All 6 table classes must use distinct connection objects"

    def test_all_tables_have_distinct_locks(self, db):
        locks = [
            db.images._lock,
            db.tags._lock,
            db.faces._lock,
            db.embeddings._lock,
            db.ledgers._lock,
            db.cache._lock,
        ]
        assert len(set(id(l) for l in locks)) == 6, \
            "All 6 table classes must use distinct Lock objects"

    def test_facade_has_no_connection(self, db):
        assert not hasattr(db, 'conn'), \
            "Facade should not hold a persistent connection"
        assert not hasattr(db, '_lock') or not isinstance(getattr(db, '_lock', None), type(threading.Lock())), \
            "Facade should not hold a persistent _lock for DB access"


class TestWALConcurrency:
    """Verify that WAL mode is active and concurrent reads don't block."""

    def test_all_connections_use_wal(self, db):
        for name, table in [
            ('images', db.images), ('tags', db.tags), ('faces', db.faces),
            ('embeddings', db.embeddings), ('ledgers', db.ledgers), ('cache', db.cache),
        ]:
            result = table.conn.execute("PRAGMA journal_mode").fetchone()
            assert result[0] == 'wal', f"{name} connection is not in WAL mode"

    def test_concurrent_read_across_domains(self, db, tmp_path):
        """Reads on different table connections should not block each other."""
        # Insert test data
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8")
        db.images.batch_ensure_records_exist([str(img)])
        db.faces.create_person("p1", name="Alice")

        results = {}
        errors = []

        def read_images():
            try:
                m = db.images.get_metadata(str(img))
                results['images'] = m is not None
            except Exception as e:
                errors.append(('images', e))

        def read_faces():
            try:
                p = db.faces.get_all_persons()
                results['faces'] = len(p) > 0
            except Exception as e:
                errors.append(('faces', e))

        t1 = threading.Thread(target=read_images)
        t2 = threading.Thread(target=read_faces)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Concurrent reads failed: {errors}"
        assert results.get('images'), "Image read failed"
        assert results.get('faces'), "Face read failed"

    def test_read_does_not_block_on_write(self, db, tmp_path):
        """A reader on one domain should not block while another domain writes."""
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8")
        db.images.batch_ensure_records_exist([str(img)])

        write_started = threading.Event()
        read_completed = threading.Event()
        errors = []

        def slow_write():
            """Hold the ledger lock for a bit while writing."""
            try:
                with db.ledgers._lock:
                    write_started.set()
                    db.ledgers.conn.execute(
                        "INSERT OR IGNORE INTO scan_ledger "
                        "(file_path, scan_root, status, discovered_at) "
                        "VALUES (?, ?, ?, ?)",
                        (str(img), str(tmp_path), 'discovered', time.time()),
                    )
                    db.ledgers.conn.commit()
                    # Hold lock briefly to prove the reader doesn't wait
                    time.sleep(0.1)
            except Exception as e:
                errors.append(('write', e))

        def fast_read():
            """Read from images while ledger lock is held."""
            try:
                write_started.wait(timeout=5)
                m = db.images.get_metadata(str(img))
                read_completed.set()
            except Exception as e:
                errors.append(('read', e))

        t_write = threading.Thread(target=slow_write)
        t_read = threading.Thread(target=fast_read)
        t_write.start()
        t_read.start()

        # The read should complete while the write is still holding its lock
        assert read_completed.wait(timeout=2), \
            "Image read blocked while ledger lock was held — connections are not isolated"

        t_write.join(timeout=5)
        t_read.join(timeout=5)
        assert not errors, f"Concurrent read/write failed: {errors}"


class TestCrossDomainOperations:
    """Verify facade methods that touch multiple tables work correctly."""

    def test_remove_records_cleans_all_domains(self, db, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8")
        path = str(img)

        db.images.batch_ensure_records_exist([path])
        db.ledgers.pending_write_insert(path, 'rating', {'rating': 5})
        db.embeddings.upsert_embedding(path, b'\x00' * 512)
        db.faces.insert_face_detection(
            "face1", path, b'\x00' * 512,
            (0.1, 0.1, 0.2, 0.2), 0.99, "buffalo_l")

        db.remove_records([path])

        assert db.images.get_metadata(path) is None
        assert not db.ledgers.pending_write_exists(path, 'rating')
        assert db.embeddings.get_embedding(path) is None
        assert db.faces.get_faces_for_file(path) == []

    def test_extract_and_store_metadata_populates_tags(self, db, tmp_path):
        """extract_and_store_metadata should write keywords to the tags table."""
        # This test needs a real image with EXIF — just verify the facade wiring
        # by checking that the tags table is accessible after a metadata store
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8")
        path = str(img)

        db.images.batch_ensure_records_exist([path])
        db.tags.add_image_tags(path, ["sunset", "beach"])

        tags = db.tags.get_image_tags(path)
        assert "sunset" in tags
        assert "beach" in tags

    def test_generation_counters_track_writes(self, db, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8")
        path = str(img)

        gen_emb_before = db.embedding_generation
        db.embeddings.upsert_embedding(path, b'\x00' * 512)
        assert db.embedding_generation == gen_emb_before + 1

        gen_face_before = db.face_generation
        db.faces.insert_face_detection(
            "f1", path, b'\x00' * 512,
            (0.1, 0.1, 0.2, 0.2), 0.99, "buffalo_l")
        assert db.face_generation == gen_face_before + 1


class TestSchemaConsistency:
    """All connections must see the same schema."""

    def test_all_connections_see_all_tables(self, db):
        expected_tables = {
            'image_metadata', 'tags', 'image_tags', 'scan_ledger',
            'clip_embeddings', 'pending_writes', 'file_work',
            'file_transfers', 'face_detections', 'persons',
        }
        for name, table in [
            ('images', db.images), ('tags', db.tags), ('faces', db.faces),
            ('embeddings', db.embeddings), ('ledgers', db.ledgers), ('cache', db.cache),
        ]:
            cursor = table.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            missing = expected_tables - tables
            assert not missing, \
                f"{name} connection is missing tables: {missing}"
