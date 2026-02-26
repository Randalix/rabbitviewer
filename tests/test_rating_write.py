"""
Tests that writing a rating via PILPlugin persists to an XMP sidecar file.

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


def _read_xmp_rating(file_path: str) -> int | None:
    """Return the XMP:Rating stored in file_path, or None if absent."""
    result = subprocess.run(
        ["exiftool", "-json", "-XMP:Rating", file_path],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    if not data:
        return None
    entry = data[0]
    raw = entry["Rating"] if "Rating" in entry else entry.get("XMP:Rating")
    return int(float(raw)) if raw is not None else None


@pytest.fixture()
def jpeg_file(tmp_path):
    """A plain JPEG with no pre-existing XMP rating."""
    path = tmp_path / "test.jpg"
    Image.new("RGB", (64, 64), color=(128, 64, 32)).save(str(path), "JPEG")
    return str(path)


@pytest.fixture()
def plugin(tmp_path):
    return PILPlugin(cache_dir=str(tmp_path / "cache"))


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
@pytest.mark.parametrize("rating", [0, 1, 2, 3, 4, 5])
def test_write_rating_persists_to_sidecar(plugin, jpeg_file, rating):
    """write_rating() must write the correct XMP:Rating value to a sidecar file."""
    success = plugin.write_rating(jpeg_file, rating)
    assert success, f"write_rating returned False for rating={rating}"

    xmp = sidecar_path_for(jpeg_file)
    assert os.path.exists(xmp), f"Sidecar {xmp} was not created"

    on_disk = _read_xmp_rating(xmp)
    assert on_disk == rating, (
        f"Expected XMP:Rating={rating} in sidecar, got {on_disk!r}"
    )

    # Original file must be untouched.
    assert _read_xmp_rating(jpeg_file) is None


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_rating_overwrites_previous(plugin, jpeg_file):
    """A second write_rating() call must replace the previous sidecar value."""
    plugin.write_rating(jpeg_file, 3)
    plugin.write_rating(jpeg_file, 5)
    xmp = sidecar_path_for(jpeg_file)
    assert _read_xmp_rating(xmp) == 5


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_rating_creates_sidecar_for_missing_file(plugin, tmp_path):
    """write_rating() on a non-existent file still creates a sidecar (exiftool -o)."""
    missing = str(tmp_path / "nonexistent.jpg")
    result = plugin.write_rating(missing, 3)
    # Sidecar creation without a source file succeeds with exiftool.
    xmp = sidecar_path_for(missing)
    assert result is True
    assert os.path.exists(xmp)
