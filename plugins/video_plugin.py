import functools
import json
import subprocess
import os
import logging
from typing import List, Optional, Dict, Any, Union
from plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = [
    '.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v',
    '.wmv', '.flv', '.mpg', '.mpeg', '.3gp', '.ts',
]


@functools.lru_cache(maxsize=1)
def _is_mpv_available() -> bool:
    try:
        subprocess.run(
            ["mpv", "--version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


@functools.lru_cache(maxsize=1)
def _is_ffprobe_available() -> bool:
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


class VideoPlugin(BasePlugin):

    # v2: switched from ffmpeg to mpv so cached frames match the hover preview.
    # v3: added --hr-seek=yes so the cached frame matches hover's precise seek.
    cache_version = 3

    def is_available(self) -> bool:
        return _is_mpv_available() and _is_ffprobe_available()

    def get_supported_formats(self) -> List[str]:
        return VIDEO_EXTENSIONS

    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """Extract a frame at ~10% of the video duration via mpv."""
        output_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(output_path):  # disk-io: cache file check
            return output_path

        seek_time = self._seek_time(image_path)
        vf = (f"scale=w={self.thumbnail_size}:h={self.thumbnail_size}:"
              f"force_original_aspect_ratio=decrease")
        return self._run_mpv_frame(image_path, output_path, seek_time, vf=vf)

    def process_view_image(self, image_path: str, md5_hash: str,
                           cancel_event=None,
                           prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """Native-size poster frame shown briefly before mpv starts rendering."""
        output_path = self.get_view_image_path(md5_hash)
        if os.path.exists(output_path):  # disk-io: cache file check
            return output_path

        seek_time = self._seek_time(image_path)
        return self._run_mpv_frame(image_path, output_path, seek_time, vf=None)

    def generate_thumbnail(self, image_path: str, image_source: Union[str, bytes],
                           orientation: int, output_path: str) -> bool:
        return self.process_thumbnail(image_path, "", None) is not None

    def generate_view_image(self, image_path: str, image_source: Union[str, bytes],
                            orientation: int, output_path: str) -> bool:
        return self.process_view_image(image_path, "") is not None

    def extract_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Use ffprobe to extract video metadata."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_format", "-show_streams",
                    file_path,
                ],
                capture_output=True, check=True, timeout=10,
            )
            data = json.loads(result.stdout)
            fmt = data.get("format", {})
            video_stream = next(
                (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
                {},
            )
            return {
                "duration": float(fmt.get("duration", 0)),
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "codec": video_stream.get("codec_name", ""),
                "video": True,
            }
        except (subprocess.SubprocessError, ValueError, KeyError) as e:
            logger.warning("ffprobe metadata failed for %s: %s", file_path, e)
            return None

    def write_rating(self, file_path: str, rating: int) -> bool:
        """Videos don't embed XMP ratings. Store in DB only."""
        return False

    def _seek_time(self, path: str) -> float:
        duration = self._get_duration(path)
        return max(duration * 0.1, 0.0) if duration else 2.0

    def _get_duration(self, path: str) -> float:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    path,
                ],
                capture_output=True, check=True, timeout=10,
            )
            return float(result.stdout.strip())
        except (subprocess.SubprocessError, ValueError):
            return 0.0

    def _run_mpv_frame(self, image_path: str, output_path: str,
                       seek_time: float, vf: Optional[str]) -> Optional[str]:
        # --no-config: ignore user mpv.conf so output is deterministic.
        # ovcopts=strict=unofficial: mjpeg encoder rejects non-full-range YUV
        # in strict mode; relaxing matches hover's decode path.
        # ofopts=update=1: single-file output without the image2 sequence warning.
        cmd = [
            "mpv", "--no-config", "--really-quiet", "--no-terminal",
            "--no-audio", "--no-sub",
            f"--start={seek_time}",
            "--hr-seek=yes",  # match hover's precise seek so the cached frame == the first hover frame
            "--frames=1",
            "--of=image2", "--ovc=mjpeg",
            "--ovcopts=strict=unofficial",
            "--ofopts=update=1",
            f"--o={output_path}",
        ]
        if vf:
            cmd.insert(-1, f"--vf={vf}")
        cmd.append(image_path)

        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=30)
            return output_path if os.path.exists(output_path) else None  # disk-io: mpv output check
        except subprocess.SubprocessError as e:
            logger.error("mpv frame extract failed for %s: %s", image_path, e)
            return None
