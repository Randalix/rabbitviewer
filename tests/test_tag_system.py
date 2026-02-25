"""Tests for the tag system: DB CRUD, junction table queries, filtered queries, and protocol."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from network import protocol


# ---------------------------------------------------------------------------
# 1. DB tag CRUD
# ---------------------------------------------------------------------------


class TestTagCRUD:

    def test_get_or_create_tag_creates(self, tmp_env):
        db = tmp_env["db"]
        with db._lock:
            tag_id = db.get_or_create_tag("landscape")
        assert tag_id > 0
        tags = db.get_all_tags()
        assert any(t["name"] == "landscape" for t in tags)

    def test_get_or_create_tag_idempotent(self, tmp_env):
        db = tmp_env["db"]
        with db._lock:
            id1 = db.get_or_create_tag("portrait")
            id2 = db.get_or_create_tag("portrait")
        assert id1 == id2

    def test_add_image_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        path = sample_images[0]
        db.batch_ensure_records_exist([path])
        db.add_image_tags(path, ["sunset", "beach"])
        tags = db.get_image_tags(path)
        assert sorted(tags) == ["beach", "sunset"]

    def test_add_image_tags_deduplicates(self, tmp_env, sample_images):
        db = tmp_env["db"]
        path = sample_images[0]
        db.batch_ensure_records_exist([path])
        db.add_image_tags(path, ["sunset"])
        db.add_image_tags(path, ["sunset", "beach"])
        tags = db.get_image_tags(path)
        assert sorted(tags) == ["beach", "sunset"]

    def test_remove_image_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        path = sample_images[0]
        db.batch_ensure_records_exist([path])
        db.add_image_tags(path, ["a", "b", "c"])
        db.remove_image_tags(path, ["b"])
        assert sorted(db.get_image_tags(path)) == ["a", "c"]

    def test_set_image_tags_replaces(self, tmp_env, sample_images):
        db = tmp_env["db"]
        path = sample_images[0]
        db.batch_ensure_records_exist([path])
        db.add_image_tags(path, ["old_tag"])
        db.set_image_tags(path, ["new_tag"])
        assert db.get_image_tags(path) == ["new_tag"]

    def test_get_image_tags_empty(self, tmp_env, sample_images):
        db = tmp_env["db"]
        path = sample_images[0]
        db.batch_ensure_records_exist([path])
        assert db.get_image_tags(path) == []


# ---------------------------------------------------------------------------
# 2. Batch operations
# ---------------------------------------------------------------------------


class TestTagBatchOps:

    def test_batch_set_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:5]
        db.batch_ensure_records_exist(paths)
        ok = db.batch_set_tags(paths, ["hero", "select"])
        assert ok is True
        for p in paths:
            assert sorted(db.get_image_tags(p)) == ["hero", "select"]

    def test_batch_remove_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:3]
        db.batch_ensure_records_exist(paths)
        db.batch_set_tags(paths, ["keep", "remove_me"])
        ok = db.batch_remove_tags(paths, ["remove_me"])
        assert ok is True
        for p in paths:
            assert db.get_image_tags(p) == ["keep"]

    def test_batch_set_empty_returns_false(self, tmp_env):
        db = tmp_env["db"]
        assert db.batch_set_tags([], ["tag"]) is False
        assert db.batch_set_tags(["path"], []) is False


# ---------------------------------------------------------------------------
# 3. Tag listing / directory scoping
# ---------------------------------------------------------------------------


class TestTagListing:

    def test_get_all_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        db.batch_ensure_records_exist(sample_images[:2])
        db.add_image_tags(sample_images[0], ["alpha"])
        db.add_image_tags(sample_images[1], ["beta"])
        names = [t["name"] for t in db.get_all_tags()]
        assert "alpha" in names
        assert "beta" in names

    def test_get_all_tags_by_kind(self, tmp_env, sample_images):
        db = tmp_env["db"]
        db.batch_ensure_records_exist([sample_images[0]])
        # Create tags with different kinds
        with db._lock:
            db.get_or_create_tag("select", kind="workflow")
            db.get_or_create_tag("landscape", kind="keyword")
        workflow = db.get_all_tags(kind="workflow")
        keyword = db.get_all_tags(kind="keyword")
        assert any(t["name"] == "select" for t in workflow)
        assert not any(t["name"] == "landscape" for t in workflow)
        assert any(t["name"] == "landscape" for t in keyword)

    def test_get_directory_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        db.batch_ensure_records_exist(sample_images[:3])
        db.add_image_tags(sample_images[0], ["dir_tag"])
        # Get tags for the parent directory
        parent = os.path.dirname(sample_images[0])
        dir_tags = db.get_directory_tags(parent)
        assert any(t["name"] == "dir_tag" for t in dir_tags)


# ---------------------------------------------------------------------------
# 4. Filtered file paths with tags
# ---------------------------------------------------------------------------


class TestFilteredFilePathsWithTags:

    def test_filter_by_tags(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:5]
        db.batch_ensure_records_exist(paths)
        db.add_image_tags(paths[0], ["hero"])
        db.add_image_tags(paths[1], ["hero", "select"])
        db.add_image_tags(paths[2], ["reject"])

        all_stars = [True, True, True, True, True, True]
        result = db.get_filtered_file_paths("", all_stars, tag_names=["hero"])
        assert sorted(result) == sorted([paths[0], paths[1]])

    def test_filter_by_tags_and_rating(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:3]
        db.batch_ensure_records_exist(paths)
        db.batch_set_ratings(paths[:2], 3)
        db.add_image_tags(paths[0], ["hero"])
        db.add_image_tags(paths[1], ["hero"])
        db.add_image_tags(paths[2], ["hero"])

        # Only rating 3 + tag "hero"
        star_states = [False, False, False, True, False, False]  # only 3-star
        result = db.get_filtered_file_paths("", star_states, tag_names=["hero"])
        assert sorted(result) == sorted([paths[0], paths[1]])

    def test_filter_no_tags_returns_all(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:3]
        db.batch_ensure_records_exist(paths)
        all_stars = [True, True, True, True, True, True]
        result = db.get_filtered_file_paths("", all_stars, tag_names=None)
        assert len(result) == 3

    def test_filter_nonexistent_tag_returns_empty(self, tmp_env, sample_images):
        db = tmp_env["db"]
        paths = sample_images[:3]
        db.batch_ensure_records_exist(paths)
        all_stars = [True, True, True, True, True, True]
        result = db.get_filtered_file_paths("", all_stars, tag_names=["nonexistent"])
        assert result == []


# ---------------------------------------------------------------------------
# 5. Protocol serialization
# ---------------------------------------------------------------------------


class TestTagProtocol:

    def test_set_tags_request_roundtrip(self):
        req = protocol.SetTagsRequest(
            image_paths=["/a.jpg", "/b.jpg"],
            tags=["sunset", "beach"],
        )
        data = req.model_dump()
        restored = protocol.SetTagsRequest.model_validate(data)
        assert restored.command == "set_tags"
        assert restored.image_paths == ["/a.jpg", "/b.jpg"]
        assert restored.tags == ["sunset", "beach"]

    def test_remove_tags_request_roundtrip(self):
        req = protocol.RemoveTagsRequest(
            image_paths=["/a.jpg"],
            tags=["old"],
        )
        data = req.model_dump()
        restored = protocol.RemoveTagsRequest.model_validate(data)
        assert restored.command == "remove_tags"
        assert restored.tags == ["old"]

    def test_get_tags_response_roundtrip(self):
        resp = protocol.GetTagsResponse(
            directory_tags=[protocol.TagInfo(name="local", kind="keyword")],
            global_tags=[protocol.TagInfo(name="global", kind="workflow")],
        )
        data = resp.model_dump()
        restored = protocol.GetTagsResponse.model_validate(data)
        assert len(restored.directory_tags) == 1
        assert restored.directory_tags[0].name == "local"
        assert len(restored.global_tags) == 1
        assert restored.global_tags[0].kind == "workflow"

    def test_get_image_tags_response_roundtrip(self):
        resp = protocol.GetImageTagsResponse(
            tags={"/a.jpg": ["sunset", "beach"], "/b.jpg": []},
        )
        data = resp.model_dump()
        restored = protocol.GetImageTagsResponse.model_validate(data)
        assert restored.tags["/a.jpg"] == ["sunset", "beach"]
        assert restored.tags["/b.jpg"] == []

    def test_filtered_request_with_tags(self):
        req = protocol.GetFilteredFilePathsRequest(
            text_filter="img",
            star_states=[True, True, True, True, True, True],
            tag_names=["hero"],
        )
        data = req.model_dump()
        restored = protocol.GetFilteredFilePathsRequest.model_validate(data)
        assert restored.tag_names == ["hero"]
