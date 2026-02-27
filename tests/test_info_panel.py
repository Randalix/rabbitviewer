"""Tests for the InfoPanel system: MetadataCache, ContentProvider, MetadataProvider."""
import threading
from unittest.mock import MagicMock

import pytest

from gui.metadata_cache import MetadataCache

# Import modules directly to avoid gui/info_panel/__init__.py which pulls in
# PySide6.QtWidgets (not stubbed in the test environment).
from gui.info_panel.content_provider import ContentProvider, Section
from gui.info_panel.metadata_provider import MetadataProvider
from gui.info_panel.script_output_provider import ScriptOutputProvider


# ---------------------------------------------------------------------------
# Section dataclass
# ---------------------------------------------------------------------------

class TestSection:
    def test_defaults(self):
        s = Section("Title")
        assert s.title == "Title"
        assert s.rows == []

    def test_with_rows(self):
        rows = [("key", "val")]
        s = Section("T", rows)
        assert s.rows == rows


# ---------------------------------------------------------------------------
# MetadataCache
# ---------------------------------------------------------------------------

class TestMetadataCache:
    def _make_cache(self, socket_client=None):
        return MetadataCache(socket_client or MagicMock())

    def test_get_miss(self):
        cache = self._make_cache()
        assert cache.get("/no/such/path") is None

    def test_put_and_get(self):
        cache = self._make_cache()
        meta = {"rating": 3, "camera_make": "Canon"}
        cache.put("/img.jpg", meta)
        assert cache.get("/img.jpg") == meta

    def test_put_overwrites(self):
        cache = self._make_cache()
        cache.put("/img.jpg", {"rating": 1})
        cache.put("/img.jpg", {"rating": 5})
        assert cache.get("/img.jpg")["rating"] == 5

    def test_put_batch(self):
        cache = self._make_cache()
        batch = {
            "/a.jpg": {"rating": 1},
            "/b.jpg": {"rating": 2},
        }
        cache.put_batch(batch)
        assert cache.get("/a.jpg")["rating"] == 1
        assert cache.get("/b.jpg")["rating"] == 2

    def test_invalidate(self):
        cache = self._make_cache()
        cache.put("/img.jpg", {"rating": 3})
        cache.invalidate("/img.jpg")
        assert cache.get("/img.jpg") is None

    def test_invalidate_missing_is_noop(self):
        cache = self._make_cache()
        cache.invalidate("/no/such")  # should not raise

    def test_lru_eviction(self):
        cache = self._make_cache()
        cache.MAX_ENTRIES = 3
        cache.put("/a.jpg", {"a": 1})
        cache.put("/b.jpg", {"b": 2})
        cache.put("/c.jpg", {"c": 3})
        cache.put("/d.jpg", {"d": 4})  # evicts /a.jpg
        assert cache.get("/a.jpg") is None
        assert cache.get("/b.jpg") is not None

    def test_lru_access_refreshes(self):
        cache = self._make_cache()
        cache.MAX_ENTRIES = 3
        cache.put("/a.jpg", {"a": 1})
        cache.put("/b.jpg", {"b": 2})
        cache.put("/c.jpg", {"c": 3})
        cache.get("/a.jpg")  # refresh /a.jpg
        cache.put("/d.jpg", {"d": 4})  # should evict /b.jpg, not /a.jpg
        assert cache.get("/a.jpg") is not None
        assert cache.get("/b.jpg") is None

    def test_fetch_and_cache_success(self):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.metadata = {"/img.jpg": {"rating": 5, "iso": 400}}
        mock_client.get_metadata_batch.return_value = resp

        cache = self._make_cache(mock_client)
        result = cache.fetch_and_cache(["/img.jpg"])

        assert result["/img.jpg"]["rating"] == 5
        assert cache.get("/img.jpg")["iso"] == 400
        mock_client.get_metadata_batch.assert_called_once_with(["/img.jpg"])

    def test_fetch_and_cache_failure(self):
        mock_client = MagicMock()
        mock_client.get_metadata_batch.side_effect = ConnectionError("boom")

        cache = self._make_cache(mock_client)
        result = cache.fetch_and_cache(["/img.jpg"])

        assert result == {}
        assert cache.get("/img.jpg") is None

    def test_fetch_and_cache_none_response(self):
        mock_client = MagicMock()
        mock_client.get_metadata_batch.return_value = None

        cache = self._make_cache(mock_client)
        result = cache.fetch_and_cache(["/img.jpg"])
        assert result == {}

    def test_thread_safety(self):
        cache = self._make_cache()
        errors = []

        def writer(start):
            try:
                for i in range(100):
                    cache.put(f"/{start + i}.jpg", {"v": i})
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cache.get(f"/{i}.jpg")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(100,)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ---------------------------------------------------------------------------
# MetadataProvider
# ---------------------------------------------------------------------------

class TestMetadataProvider:
    def _make_provider(self, cache_data=None):
        cache = MagicMock()
        cache.get.side_effect = lambda p: (cache_data or {}).get(p)
        return MetadataProvider(cache)

    def test_provider_name(self):
        p = self._make_provider()
        assert p.provider_name == "Metadata"

    def test_cache_miss(self):
        p = self._make_provider()
        sections = p.get_sections("/missing.jpg")
        assert len(sections) == 1
        assert sections[0].title == "Status"
        assert "No metadata" in sections[0].rows[0][1]

    def test_file_section_always_present(self):
        p = self._make_provider({"/img.jpg": {"rating": 0}})
        sections = p.get_sections("/img.jpg")
        titles = [s.title for s in sections]
        assert "File" in titles

    def test_filename_in_file_section(self):
        p = self._make_provider({"/path/to/photo.jpg": {"rating": 0}})
        sections = p.get_sections("/path/to/photo.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        assert ("Filename", "photo.jpg") in file_sec.rows

    def test_dimensions(self):
        p = self._make_provider({"/img.jpg": {"width": 6000, "height": 4000}})
        sections = p.get_sections("/img.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        dim_rows = [r for r in file_sec.rows if r[0] == "Dimensions"]
        assert len(dim_rows) == 1
        assert "6000 x 4000" in dim_rows[0][1]

    def test_zero_dimensions_hidden(self):
        p = self._make_provider({"/img.jpg": {"width": 0, "height": 0}})
        sections = p.get_sections("/img.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        dim_rows = [r for r in file_sec.rows if r[0] == "Dimensions"]
        assert dim_rows == []

    def test_file_size(self):
        p = self._make_provider({"/img.jpg": {"file_size": 10 * 1024 * 1024}})
        sections = p.get_sections("/img.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        size_rows = [r for r in file_sec.rows if r[0] == "File Size"]
        assert "10.0 MB" in size_rows[0][1]

    def test_rating_stars(self):
        p = self._make_provider({"/img.jpg": {"rating": 3}})
        sections = p.get_sections("/img.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        rating_rows = [r for r in file_sec.rows if r[0] == "Rating"]
        assert rating_rows[0][1] == "\u2605\u2605\u2605"

    def test_zero_rating_hidden(self):
        p = self._make_provider({"/img.jpg": {"rating": 0}})
        sections = p.get_sections("/img.jpg")
        file_sec = [s for s in sections if s.title == "File"][0]
        rating_rows = [r for r in file_sec.rows if r[0] == "Rating"]
        assert rating_rows == []

    def test_camera_section(self):
        p = self._make_provider({"/img.jpg": {
            "camera_make": "Canon",
            "camera_model": "EOS R5",
            "lens_model": "RF 85mm F1.2L",
        }})
        sections = p.get_sections("/img.jpg")
        cam_sec = [s for s in sections if s.title == "Camera"][0]
        assert ("Make", "Canon") in cam_sec.rows
        assert ("Model", "EOS R5") in cam_sec.rows
        assert ("Lens", "RF 85mm F1.2L") in cam_sec.rows

    def test_camera_section_absent_when_all_none(self):
        p = self._make_provider({"/img.jpg": {
            "camera_make": None, "camera_model": None, "lens_model": None,
        }})
        sections = p.get_sections("/img.jpg")
        titles = [s.title for s in sections]
        assert "Camera" not in titles

    def test_exposure_section(self):
        p = self._make_provider({"/img.jpg": {
            "focal_length": 85.0,
            "aperture": 1.2,
            "shutter_speed": "1/500",
            "iso": 400,
            "date_taken": 1728907200.0,
        }})
        sections = p.get_sections("/img.jpg")
        exp_sec = [s for s in sections if s.title == "Exposure"][0]
        row_dict = dict(exp_sec.rows)
        assert row_dict["Focal Length"] == "85.0mm"
        assert row_dict["Aperture"] == "f/1.2"
        assert row_dict["Shutter Speed"] == "1/500"
        assert row_dict["ISO"] == "400"
        from datetime import datetime
        expected = datetime.fromtimestamp(1728907200.0).strftime("%Y-%m-%d %H:%M:%S")
        assert row_dict["Date"] == expected

    def test_exposure_section_absent_when_all_none(self):
        p = self._make_provider({"/img.jpg": {
            "focal_length": None, "aperture": None,
            "shutter_speed": None, "iso": None, "date_taken": None,
        }})
        sections = p.get_sections("/img.jpg")
        titles = [s.title for s in sections]
        assert "Exposure" not in titles

    def test_partial_exposure(self):
        """Only aperture present — section should appear with just that row."""
        p = self._make_provider({"/img.jpg": {
            "aperture": 2.8,
        }})
        sections = p.get_sections("/img.jpg")
        exp_sec = [s for s in sections if s.title == "Exposure"][0]
        assert len(exp_sec.rows) == 1
        assert exp_sec.rows[0] == ("Aperture", "f/2.8")

    def test_full_metadata(self):
        """All fields populated — should produce all 3 sections."""
        meta = {
            "width": 6000, "height": 4000, "file_size": 20_000_000, "rating": 5,
            "camera_make": "Canon", "camera_model": "R5", "lens_model": "RF 85",
            "focal_length": 85.0, "aperture": 1.2, "shutter_speed": "1/500",
            "iso": 100, "date_taken": 1735689600.0,
        }
        p = self._make_provider({"/img.jpg": meta})
        sections = p.get_sections("/img.jpg")
        titles = [s.title for s in sections]
        assert titles == ["File", "Camera", "Exposure"]


# ---------------------------------------------------------------------------
# ScriptOutputProvider
# ---------------------------------------------------------------------------

class TestScriptOutputProvider:
    def test_provider_name(self):
        p = ScriptOutputProvider()
        assert p.provider_name == "Script Output"

    def test_no_output(self):
        p = ScriptOutputProvider()
        sections = p.get_sections("/img.jpg")
        assert len(sections) == 1
        assert "No script output" in sections[0].rows[0][1]

    def test_receive_and_display(self):
        p = ScriptOutputProvider()
        p.receive_output("/img.jpg", "Score", "95")
        p.receive_output("/img.jpg", "Tag", "landscape")
        sections = p.get_sections("/img.jpg")
        assert sections[0].title == "Script Output"
        assert ("Score", "95") in sections[0].rows
        assert ("Tag", "landscape") in sections[0].rows

    def test_per_image_isolation(self):
        p = ScriptOutputProvider()
        p.receive_output("/a.jpg", "k", "v1")
        p.receive_output("/b.jpg", "k", "v2")
        assert p.get_sections("/a.jpg")[0].rows == [("k", "v1")]
        assert p.get_sections("/b.jpg")[0].rows == [("k", "v2")]


# ---------------------------------------------------------------------------
# ContentProvider ABC
# ---------------------------------------------------------------------------

class TestContentProviderABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            ContentProvider()

    def test_subclass_works(self):
        class Dummy(ContentProvider):
            @property
            def provider_name(self):
                return "Dummy"
            def get_sections(self, image_path):
                return [Section("Test", [("a", "b")])]

        d = Dummy()
        assert d.provider_name == "Dummy"
        assert d.get_sections("/x")[0].rows == [("a", "b")]
        d.on_cleanup()  # should not raise
