"""Tests for core.file_ops — sidecar-aware file operations."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestResolveSidecars:
    def test_with_xmp(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        from core.file_ops import resolve_sidecars
        assert resolve_sidecars(str(img)) == [str(xmp)]

    def test_without_xmp(self, tmp_path):
        img = tmp_path / "photo.cr3"
        img.write_text("image")

        from core.file_ops import resolve_sidecars
        assert resolve_sidecars(str(img)) == []

    def test_jpeg_extension(self, tmp_path):
        img = tmp_path / "photo.JPG"
        xmp = tmp_path / "photo.JPG.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        from core.file_ops import resolve_sidecars
        assert resolve_sidecars(str(img)) == [str(xmp)]


class TestTrashWithSidecars:
    def _patch_send2trash(self, side_effect):
        """Patch the lazy send2trash loader to return a mock."""
        mock_fn = MagicMock(side_effect=side_effect)
        return patch("core.file_ops._get_send2trash", return_value=mock_fn), mock_fn

    def test_trashes_image_and_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        trashed = []
        patcher, _ = self._patch_send2trash(lambda p: trashed.append(p))
        with patcher:
            from core.file_ops import trash_with_sidecars
            result = trash_with_sidecars([str(img)])

        assert result["succeeded"] == 1
        assert result["failed"] == 0
        assert str(img) in trashed
        assert str(xmp) in trashed

    def test_no_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        img.write_text("image")

        trashed = []
        patcher, _ = self._patch_send2trash(lambda p: trashed.append(p))
        with patcher:
            from core.file_ops import trash_with_sidecars
            result = trash_with_sidecars([str(img)])

        assert result["succeeded"] == 1
        assert result["failed"] == 0
        assert trashed == [str(img)]

    def test_sidecar_failure_nonfatal(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        def mock_trash(p):
            if p.endswith(".xmp"):
                raise OSError("permission denied")

        patcher, _ = self._patch_send2trash(mock_trash)
        with patcher:
            from core.file_ops import trash_with_sidecars
            result = trash_with_sidecars([str(img)])

        # Image succeeded despite sidecar failure
        assert result["succeeded"] == 1
        assert result["failed"] == 0

    def test_image_failure(self, tmp_path):
        img = tmp_path / "photo.cr3"
        img.write_text("image")

        patcher, _ = self._patch_send2trash(OSError("nope"))
        with patcher:
            from core.file_ops import trash_with_sidecars
            result = trash_with_sidecars([str(img)])

        assert result["succeeded"] == 0
        assert result["failed"] == 1


class TestRemoveWithSidecars:
    def test_removes_image_and_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        from core.file_ops import remove_with_sidecars
        remove_with_sidecars([str(img)])

        assert not img.exists()
        assert not xmp.exists()

    def test_removes_image_without_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        img.write_text("image")

        from core.file_ops import remove_with_sidecars
        remove_with_sidecars([str(img)])

        assert not img.exists()


class TestImageEntry:
    def test_from_path_with_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        from core.priority import ImageEntry
        entry = ImageEntry.from_path(str(img))
        assert entry.path == str(img)
        assert entry.sidecars == (str(xmp),)
        assert entry.variant is None

    def test_from_path_without_sidecar(self, tmp_path):
        img = tmp_path / "photo.cr3"
        img.write_text("image")

        from core.priority import ImageEntry
        entry = ImageEntry.from_path(str(img))
        assert entry.path == str(img)
        assert entry.sidecars == ()

    def test_all_files(self, tmp_path):
        img = tmp_path / "photo.cr3"
        xmp = tmp_path / "photo.cr3.xmp"
        img.write_text("image")
        xmp.write_text("sidecar")

        from core.priority import ImageEntry
        entry = ImageEntry.from_path(str(img))
        assert entry.all_files() == (str(img), str(xmp))

    def test_frozen_and_hashable(self):
        from core.priority import ImageEntry
        e1 = ImageEntry(path="/a.jpg", sidecars=())
        e2 = ImageEntry(path="/a.jpg", sidecars=())
        assert e1 == e2
        assert hash(e1) == hash(e2)
        # Can be used in sets/dicts
        s = {e1, e2}
        assert len(s) == 1

    def test_identity_ignores_sidecars(self):
        """Same path + different sidecars → equal, same hash."""
        from core.priority import ImageEntry
        e1 = ImageEntry(path="/a.jpg", sidecars=())
        e2 = ImageEntry(path="/a.jpg", sidecars=("/a.jpg.xmp",))
        assert e1 == e2
        assert hash(e1) == hash(e2)
        assert len({e1, e2}) == 1

    def test_identity_respects_variant(self):
        from core.priority import ImageEntry
        e1 = ImageEntry(path="/a.jpg", variant=None)
        e2 = ImageEntry(path="/a.jpg", variant="v2")
        assert e1 != e2
        assert hash(e1) != hash(e2)

    def test_to_dict_minimal(self):
        from core.priority import ImageEntry
        e = ImageEntry(path="/a.jpg")
        assert e.to_dict() == {"path": "/a.jpg"}

    def test_to_dict_full(self):
        from core.priority import ImageEntry
        e = ImageEntry(path="/a.jpg", sidecars=("/a.jpg.xmp",), variant="v2")
        d = e.to_dict()
        assert d == {"path": "/a.jpg", "sidecars": ["/a.jpg.xmp"], "variant": "v2"}

    def test_from_dict_bare_string(self):
        from core.priority import ImageEntry
        e = ImageEntry.from_dict("/a.jpg")
        assert e.path == "/a.jpg"
        assert e.sidecars == ()

    def test_from_dict_dict(self):
        from core.priority import ImageEntry
        e = ImageEntry.from_dict({"path": "/a.jpg", "sidecars": ["/a.jpg.xmp"], "variant": "v2"})
        assert e.path == "/a.jpg"
        assert e.sidecars == ("/a.jpg.xmp",)
        assert e.variant == "v2"

    def test_from_dict_passthrough(self):
        from core.priority import ImageEntry
        orig = ImageEntry(path="/a.jpg")
        assert ImageEntry.from_dict(orig) is orig

    def test_roundtrip_to_from_dict(self):
        from core.priority import ImageEntry
        e = ImageEntry(path="/a.jpg", sidecars=("/a.jpg.xmp",), variant="v2")
        restored = ImageEntry.from_dict(e.to_dict())
        assert restored.path == e.path
        assert restored.sidecars == e.sidecars
        assert restored.variant == e.variant
