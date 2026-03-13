"""Tests for face recognition tables in MetadataDatabase."""
import struct

from core.metadata_database import MetadataDatabase


def _fake_embedding(dim=512, val=0.1):
    """Create a fake float32 embedding blob."""
    return struct.pack(f'{dim}f', *([val] * dim))


class TestFaceDetectionCRUD:
    def test_insert_and_get(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.insert_face_detection(
            "face-1", "/test/img.jpg", _fake_embedding(),
            (0.1, 0.2, 0.3, 0.4), 0.95, "buffalo_l")
        faces = db.get_faces_for_file("/test/img.jpg")
        assert len(faces) == 1
        assert faces[0]['face_id'] == "face-1"
        assert faces[0]['confidence'] == 0.95
        assert faces[0]['bbox'] == (0.1, 0.2, 0.3, 0.4)

    def test_multiple_faces_per_file(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.insert_face_detection("f1", "/img.jpg", _fake_embedding(val=0.1),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.insert_face_detection("f2", "/img.jpg", _fake_embedding(val=0.2),
                                 (0.5, 0.5, 0.2, 0.2), 0.85, "buffalo_l")
        faces = db.get_faces_for_file("/img.jpg")
        assert len(faces) == 2

    def test_get_faces_for_empty_file(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.get_faces_for_file("/no/such/file.jpg") == []


class TestPersonCRUD:
    def test_create_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.create_person("p1", "Alice", "face-1")
        persons = db.get_all_persons()
        assert len(persons) == 1
        assert persons[0]['name'] == "Alice"
        assert persons[0]['person_id'] == "p1"

    def test_rename_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.rename_person("p1", "Bob")
        persons = db.get_all_persons()
        assert persons[0]['name'] == "Bob"

    def test_hide_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.hide_person("p1", True)
        assert db.get_all_persons() == []
        assert len(db.get_all_persons(include_hidden=True)) == 1


class TestFaceAssignment:
    def test_assign_face_to_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.insert_face_detection("f1", "/img.jpg", _fake_embedding(),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.assign_face_to_person("f1", "p1")

        faces = db.get_faces_for_file("/img.jpg")
        assert faces[0]['person_id'] == "p1"

        persons = db.get_all_persons()
        assert persons[0]['face_count'] == 1

    def test_get_face_paths_for_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.insert_face_detection("f1", "/a.jpg", _fake_embedding(val=0.1),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.insert_face_detection("f2", "/b.jpg", _fake_embedding(val=0.2),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.assign_face_to_person("f1", "p1")
        db.assign_face_to_person("f2", "p1")
        paths = db.get_face_paths_for_person("p1")
        assert set(paths) == {"/a.jpg", "/b.jpg"}


class TestMergePersons:
    def test_merge_reassigns_faces(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.create_person("p2", "Bob")
        db.insert_face_detection("f1", "/a.jpg", _fake_embedding(val=0.1),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.insert_face_detection("f2", "/b.jpg", _fake_embedding(val=0.2),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.assign_face_to_person("f1", "p1")
        db.assign_face_to_person("f2", "p2")

        db.merge_persons("p1", ["p2"])

        # All faces should now be on p1
        persons = db.get_all_persons()
        assert len(persons) == 1
        assert persons[0]['person_id'] == "p1"
        assert persons[0]['face_count'] == 2

        # p2 should be gone
        faces_p2 = db.get_faces_for_person("p2")
        assert len(faces_p2) == 0


class TestCascadeDelete:
    def test_remove_records_deletes_faces(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        fp = "/test/img.jpg"
        # Create a minimal image_metadata record so remove_records has something to delete
        with db._lock:
            cursor = db.conn.cursor()
            cursor.execute("""
                INSERT INTO image_metadata (file_path, path_hash, mtime, created_at, updated_at)
                VALUES (?, 'hash', 0, 0, 0)
            """, (fp,))
            db.conn.commit()
        db.insert_face_detection("f1", fp, _fake_embedding(),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        assert len(db.get_faces_for_file(fp)) == 1

        db.remove_records([fp])
        assert db.get_faces_for_file(fp) == []


class TestGetFilesMissingFaces:
    def test_returns_missing(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.insert_face_detection("f1", "/a.jpg", _fake_embedding(),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        missing = db.get_files_missing_faces(["/a.jpg", "/b.jpg", "/c.jpg"])
        assert set(missing) == {"/b.jpg", "/c.jpg"}

    def test_empty_input(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.get_files_missing_faces([]) == []


class TestGetFacePathsForPersons:
    def test_union_paths(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.create_person("p2", "Bob")
        db.insert_face_detection("f1", "/a.jpg", _fake_embedding(val=0.1),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.insert_face_detection("f2", "/b.jpg", _fake_embedding(val=0.2),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.insert_face_detection("f3", "/c.jpg", _fake_embedding(val=0.3),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.assign_face_to_person("f1", "p1")
        db.assign_face_to_person("f2", "p2")
        db.assign_face_to_person("f3", "p1")
        paths = db.get_face_paths_for_persons(["p1", "p2"])
        assert set(paths) == {"/a.jpg", "/b.jpg", "/c.jpg"}

    def test_empty_ids(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        assert db.get_face_paths_for_persons([]) == []

    def test_single_person(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.insert_face_detection("f1", "/a.jpg", _fake_embedding(),
                                 (0.1, 0.1, 0.2, 0.2), 0.9, "buffalo_l")
        db.assign_face_to_person("f1", "p1")
        paths = db.get_face_paths_for_persons(["p1"])
        assert paths == ["/a.jpg"]


class TestPersonNames:
    def test_get_person_names(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        db.create_person("p2", "Bob")
        db.create_person("p3", "")  # unnamed
        names = db.get_person_names()
        assert names == ["Alice", "Bob"]

    def test_get_person_by_name(self, tmp_env):
        db: MetadataDatabase = tmp_env["db"]
        db.create_person("p1", "Alice")
        person = db.get_person_by_name("Alice")
        assert person is not None
        assert person['person_id'] == "p1"
        assert db.get_person_by_name("NoSuchPerson") is None
