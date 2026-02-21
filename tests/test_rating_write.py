"""
Tests that writing a rating via PILPlugin actually persists to the file on disk.

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
def test_write_rating_persists_to_disk(plugin, jpeg_file, rating):
    """write_rating() must embed the correct XMP:Rating value in the file."""
    success = plugin.write_rating(jpeg_file, rating)
    assert success, f"write_rating returned False for rating={rating}"

    on_disk = _read_xmp_rating(jpeg_file)
    assert on_disk == rating, (
        f"Expected XMP:Rating={rating} on disk, got {on_disk!r}"
    )


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_rating_overwrites_previous(plugin, jpeg_file):
    """A second write_rating() call must replace the previous value."""
    plugin.write_rating(jpeg_file, 3)
    plugin.write_rating(jpeg_file, 5)
    assert _read_xmp_rating(jpeg_file) == 5


@pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
def test_write_rating_returns_false_for_missing_file(plugin, tmp_path):
    """write_rating() must return False (not raise) when the file does not exist."""
    missing = str(tmp_path / "nonexistent.jpg")
    result = plugin.write_rating(missing, 3)
    assert result is False
