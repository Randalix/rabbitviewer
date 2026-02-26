"""Tests for XMP sidecar file support.

Covers utility functions, write paths (create/update), read path overrides,
and watchdog integration for .xmp file events.
"""

import os
import time
from unittest.mock import MagicMock

import pytest

from plugins.base_plugin import sidecar_path_for, find_image_for_sidecar


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

class TestSidecarPathFor:
    def test_jpg(self):
        assert sidecar_path_for("/photos/img.jpg") == "/photos/img.jpg.xmp"

    def test_cr3(self):
        assert sidecar_path_for("/photos/raw.CR3") == "/photos/raw.CR3.xmp"

    def test_png(self):
        assert sidecar_path_for("/a/b/c.png") == "/a/b/c.png.xmp"

    def test_no_extension(self):
        assert sidecar_path_for("/photos/file") == "/photos/file.xmp"

    def test_multiple_dots(self):
        assert sidecar_path_for("/photos/my.file.tiff") == "/photos/my.file.tiff.xmp"


class TestFindImageForSidecar:
    def test_finds_existing_image(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")
        result = find_image_for_sidecar(xmp, {".jpg", ".png"})
        assert result == str(img)

    def test_returns_none_when_no_image(self, tmp_path):
        xmp = str(tmp_path / "photo.jpg.xmp")
        result = find_image_for_sidecar(xmp, {".jpg", ".png"})
        assert result is None

    def test_not_xmp_extension(self):
        result = find_image_for_sidecar("/photos/photo.jpg", {".jpg"})
        assert result is None

    def test_unsupported_extension(self, tmp_path):
        # .bmp is not in supported set — should return None even if file exists.
        img = tmp_path / "photo.bmp"
        img.write_bytes(b"BM")
        xmp = str(tmp_path / "photo.bmp.xmp")
        result = find_image_for_sidecar(xmp, {".jpg", ".png"})
        assert result is None


# ---------------------------------------------------------------------------
# Write path — sidecar creation and update
# ---------------------------------------------------------------------------

class TestSidecarWrite:
    """Tests that write_rating/write_tags target sidecar files."""

    def _make_plugin(self):
        """Return a BasePlugin subclass instance with a mock exiftool."""
        from plugins.base_plugin import BasePlugin

        class FakePlugin(BasePlugin):
            def is_available(self):
                return True

            def get_supported_formats(self):
                return [".fake"]

            def generate_view_image(self, *a, **kw):
                return False

            def generate_thumbnail(self, *a, **kw):
                return False

            def process_thumbnail(self, *a, **kw):
                return None

            def process_view_image(self, *a, **kw):
                return None

        plugin = FakePlugin.__new__(FakePlugin)
        plugin._local = type("Local", (), {})()
        mock_et = MagicMock()
        plugin._local.proc = mock_et
        return plugin, mock_et

    def test_write_rating_creates_sidecar(self, tmp_path):
        plugin, mock_et = self._make_plugin()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")

        mock_et.execute.return_value = b"    1 image files created"
        result = plugin.write_rating(str(img), 3)

        assert result is True
        # Should use -o to create the sidecar from the source image.
        call_args = mock_et.execute.call_args[0][0]
        assert "-o" in call_args
        assert xmp in call_args

    def test_write_rating_updates_existing_sidecar(self, tmp_path):
        plugin, mock_et = self._make_plugin()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp_file = tmp_path / "photo.jpg.xmp"
        xmp_file.write_text("<xmp/>")

        mock_et.execute.return_value = b"    1 image files updated"
        result = plugin.write_rating(str(img), 4)

        assert result is True
        call_args = mock_et.execute.call_args[0][0]
        assert "-overwrite_original" in call_args
        assert str(xmp_file) in call_args
        assert "-o" not in call_args

    def test_write_tags_creates_sidecar(self, tmp_path):
        plugin, mock_et = self._make_plugin()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")

        mock_et.execute.return_value = b"    1 image files created"
        result = plugin.write_tags(str(img), ["bird", "nature"])

        assert result is True
        call_args = mock_et.execute.call_args[0][0]
        assert "-o" in call_args
        assert xmp in call_args

    def test_write_rating_race_retry(self, tmp_path):
        """If -o fails because sidecar appeared (race), retry with update path."""
        plugin, mock_et = self._make_plugin()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp_file = tmp_path / "photo.jpg.xmp"

        def side_effect(args):
            if "-o" in args:
                # Simulate race: file appeared after our os.path.exists check.
                xmp_file.write_text("<xmp/>")
                return b"Error: 'photo.jpg.xmp' already exists\n    0 image files updated"
            return b"    1 image files updated"

        mock_et.execute.side_effect = side_effect
        result = plugin.write_rating(str(img), 3)

        assert result is True
        assert mock_et.execute.call_count == 2


# ---------------------------------------------------------------------------
# Read path — sidecar override in extract_metadata
# ---------------------------------------------------------------------------

class TestSidecarReadOverride:
    """Tests that extract_metadata prefers sidecar values."""

    def _make_xmp(self, path, rating=None):
        """Write a minimal XMP sidecar file."""
        parts = ['<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>',
                 '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
                 '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
                 '<rdf:Description rdf:about=""',
                 ' xmlns:xmp="http://ns.adobe.com/xap/1.0/">']
        if rating is not None:
            parts.append(f'<xmp:Rating>{rating}</xmp:Rating>')
        parts.extend(['</rdf:Description>', '</rdf:RDF>',
                      '</x:xmpmeta>', '<?xpacket end="w"?>'])
        with open(path, "w") as f:
            f.write("\n".join(parts))

    def test_sidecar_overrides_embedded_rating(self, tmp_path):
        from plugins.base_plugin import BasePlugin

        # Create a minimal JPEG with embedded XMP rating=1
        img = tmp_path / "photo.jpg"
        xmp_embedded = (
            b'\xff\xd8\xff\xe1\x00\x00'
            b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            b'<rdf:Description rdf:about=""'
            b' xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            b'<xmp:Rating>1</xmp:Rating>'
            b'</rdf:Description></rdf:RDF>'
            b'</x:xmpmeta><?xpacket end="w"?>'
        )
        img.write_bytes(xmp_embedded)

        # Create a sidecar with rating=5 (double-extension convention)
        self._make_xmp(str(tmp_path / "photo.jpg.xmp"), rating=5)

        # Use a concrete subclass to call extract_metadata
        class FakePlugin(BasePlugin):
            def is_available(self): return True
            def get_supported_formats(self): return [".fake"]
            def generate_view_image(self, *a, **kw): return False
            def generate_thumbnail(self, *a, **kw): return False
            def process_thumbnail(self, *a, **kw): return None
            def process_view_image(self, *a, **kw): return None

        plugin = FakePlugin.__new__(FakePlugin)
        result = plugin.extract_metadata(str(img))

        assert result is not None
        assert result["rating"] == 5

    def test_no_sidecar_uses_embedded(self, tmp_path):
        from plugins.base_plugin import BasePlugin

        img = tmp_path / "photo.jpg"
        xmp_embedded = (
            b'\xff\xd8\xff\xe1\x00\x00'
            b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            b'<rdf:Description rdf:about=""'
            b' xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            b'<xmp:Rating>2</xmp:Rating>'
            b'</rdf:Description></rdf:RDF>'
            b'</x:xmpmeta><?xpacket end="w"?>'
        )
        img.write_bytes(xmp_embedded)

        class FakePlugin(BasePlugin):
            def is_available(self): return True
            def get_supported_formats(self): return [".fake"]
            def generate_view_image(self, *a, **kw): return False
            def generate_thumbnail(self, *a, **kw): return False
            def process_thumbnail(self, *a, **kw): return None
            def process_view_image(self, *a, **kw): return None

        plugin = FakePlugin.__new__(FakePlugin)
        result = plugin.extract_metadata(str(img))

        assert result is not None
        assert result["rating"] == 2


# ---------------------------------------------------------------------------
# Watchdog — .xmp event handling
# ---------------------------------------------------------------------------

class TestWatchdogSidecar:
    """Tests that the watchdog correctly handles .xmp file events."""

    def _make_handler(self):
        from filewatcher.watcher import WatchdogHandler
        tm = MagicMock()
        tm.render_manager = MagicMock()
        tm.metadata_db = MagicMock()
        tm.plugin_registry = MagicMock()
        tm.plugin_registry.get_supported_formats.return_value = {".jpg", ".png", ".cr3"}
        handler = WatchdogHandler(tm, [])
        return handler, tm

    def _make_event(self, event_type, src_path, is_directory=False):
        ev = MagicMock()
        ev.event_type = event_type
        ev.src_path = src_path
        ev.dest_path = src_path
        ev.is_directory = is_directory
        return ev

    def test_xmp_created_triggers_reread(self, tmp_path):
        handler, tm = self._make_handler()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")

        handler.dispatch(self._make_event("created", xmp))

        tm.render_manager.submit_task.assert_called_once()
        call_args = tm.render_manager.submit_task.call_args
        assert "sidecar_reread" in call_args[0][0]

    def test_xmp_modified_triggers_reread(self, tmp_path):
        handler, tm = self._make_handler()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")

        handler.dispatch(self._make_event("modified", xmp))

        tm.render_manager.submit_task.assert_called_once()

    def test_xmp_deleted_ignored(self, tmp_path):
        handler, tm = self._make_handler()
        xmp = str(tmp_path / "photo.jpg.xmp")

        handler.dispatch(self._make_event("deleted", xmp))

        # .xmp deletions should not trigger any tasks.
        tm.render_manager.submit_task.assert_not_called()

    def test_xmp_no_matching_image_ignored(self, tmp_path):
        handler, tm = self._make_handler()
        # No image file exists — sidecar for a missing image.
        xmp = str(tmp_path / "orphan.jpg.xmp")

        handler.dispatch(self._make_event("created", xmp))

        tm.render_manager.submit_task.assert_not_called()

    def test_xmp_self_write_suppressed(self, tmp_path):
        handler, tm = self._make_handler()
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        xmp = str(tmp_path / "photo.jpg.xmp")

        handler.ignore_next_modification(xmp)
        handler.dispatch(self._make_event("created", xmp))
        handler.dispatch(self._make_event("modified", xmp))

        tm.render_manager.submit_task.assert_not_called()
