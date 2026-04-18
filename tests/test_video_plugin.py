"""Tests for plugins/video_plugin.py (mpv-based frame extraction).

Skipped when mpv or ffmpeg is not on PATH.
"""
import os
import shutil
import subprocess

import pytest

from plugins.video_plugin import VideoPlugin

pytestmark = pytest.mark.skipif(
    not shutil.which("mpv") or not shutil.which("ffmpeg"),
    reason="mpv and ffmpeg must be on PATH",
)


@pytest.fixture()
def sample_video(tmp_path):
    """Generate a short synthetic mp4 via ffmpeg lavfi source."""
    out = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=duration=2:size=320x240:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out),
        ],
        capture_output=True, check=True, timeout=30,
    )
    return str(out)


def test_plugin_reports_available(tmp_path):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=128)
    assert plugin.is_available() is True


def test_thumbnail_written_to_cache(tmp_path, sample_video):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=128)
    thumb = plugin.process_thumbnail(sample_video, "abc123")

    assert thumb is not None
    assert os.path.exists(thumb)
    assert thumb.endswith("/thumbnails/abc123.jpg")
    assert os.path.getsize(thumb) > 0


def test_view_image_written_to_cache(tmp_path, sample_video):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=128)
    view = plugin.process_view_image(sample_video, "abc123")

    assert view is not None
    assert os.path.exists(view)
    assert view.endswith("/images/abc123.jpg")
    assert os.path.getsize(view) > 0


def test_thumbnail_respects_size_cap(tmp_path, sample_video):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=64)
    thumb = plugin.process_thumbnail(sample_video, "size_test")
    assert thumb is not None

    # Parse JPEG dimensions via `file` — both axes must fit in thumbnail_size.
    out = subprocess.run(["file", thumb], capture_output=True, text=True, check=True).stdout
    # e.g. "... 64x48, components 3"
    import re
    m = re.search(r"(\d+)x(\d+)", out)
    assert m, f"could not parse dims from: {out}"
    w, h = int(m.group(1)), int(m.group(2))
    assert w <= 64 and h <= 64


def test_cache_hit_short_circuits(tmp_path, sample_video):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=128)
    first = plugin.process_thumbnail(sample_video, "cache_test")
    assert first is not None
    mtime_before = os.path.getmtime(first)

    second = plugin.process_thumbnail(sample_video, "cache_test")
    assert second is not None and second == first
    # Cache hit must not re-run mpv (file mtime unchanged).
    assert os.path.getmtime(second) == mtime_before


def test_metadata_still_uses_ffprobe(tmp_path, sample_video):
    plugin = VideoPlugin(cache_dir=str(tmp_path), thumbnail_size=128)
    meta = plugin.extract_metadata(sample_video)
    assert meta is not None
    assert meta["video"] is True
    assert meta["width"] == 320
    assert meta["height"] == 240
    assert meta["duration"] > 0
