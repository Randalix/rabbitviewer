"""
Tests that writing tags via PILPlugin persists to an XMP sidecar file.

Requires exiftool to be installed. Tests are skipped if it is missing.
"""
import json
import shutil
import subprocess
import sys
import os
import pytest
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from plugins.pil_plugin import PILPlugin
from plugins.base_plugin import sidecar_path_for


def _exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _read_xmp_subjects(file_path: str) -> list[str]:
    """Return the XMP:Subject tags stored in file_path, or [] if absent."""
    result = subprocess.run(
        ["exiftool", "-json", "-XMP:Subject", file_path],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    if not data:
        return []
    entry = data[0]
    raw = entry.get("Subject", entry.get("XMP:Subject"))
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return sorted(raw)


@pytest.fixture()
def jpeg_file(tmp_path):
    """A plain JPEG with no pre-existing XMP tags."""
    path = tmp_path / "test.jpg"
    Image.new("RGB", (64, 64), color=(128, 64, 32)).save(str(path), "JPEG")
    return str(path)


@pytest.fixture()
def plugin(tmp_path):
    return PILPlugin(cache_dir=str(tmp_path / "cache"))


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_tags_creates_sidecar(plugin, jpeg_file):
    """write_tags() must create an XMP sidecar with the correct Subject values."""
    tags = ["bird", "nature"]
    success = plugin.write_tags(jpeg_file, tags)
    assert success, "write_tags returned False"

    xmp = sidecar_path_for(jpeg_file)
    assert os.path.exists(xmp), f"Sidecar {xmp} was not created"

    on_disk = _read_xmp_subjects(xmp)
    assert on_disk == sorted(tags), (
        f"Expected XMP:Subject={sorted(tags)} in sidecar, got {on_disk!r}"
    )


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_tags_single_tag(plugin, jpeg_file):
    """A single tag still round-trips correctly."""
    success = plugin.write_tags(jpeg_file, ["landscape"])
    assert success

    xmp = sidecar_path_for(jpeg_file)
    assert os.path.exists(xmp)
    assert _read_xmp_subjects(xmp) == ["landscape"]


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_tags_replaces_previous(plugin, jpeg_file):
    """A second write_tags() call must replace previous sidecar tags entirely."""
    plugin.write_tags(jpeg_file, ["bird", "nature"])
    plugin.write_tags(jpeg_file, ["portrait", "studio"])

    xmp = sidecar_path_for(jpeg_file)
    on_disk = _read_xmp_subjects(xmp)
    assert on_disk == ["portrait", "studio"]


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_tags_empty_clears_sidecar(plugin, jpeg_file):
    """Writing an empty tag list should clear existing tags in the sidecar."""
    plugin.write_tags(jpeg_file, ["bird"])
    plugin.write_tags(jpeg_file, [])

    xmp = sidecar_path_for(jpeg_file)
    assert os.path.exists(xmp)
    assert _read_xmp_subjects(xmp) == []


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_tags_does_not_modify_original(plugin, jpeg_file):
    """The original image file must remain untouched after writing tags."""
    plugin.write_tags(jpeg_file, ["bird", "nature"])
    assert _read_xmp_subjects(jpeg_file) == []
