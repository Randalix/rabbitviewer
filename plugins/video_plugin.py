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
def _is_ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


class VideoPlugin(BasePlugin):

    def is_available(self) -> bool:
        return _is_ffmpeg_available()

    def get_supported_formats(self) -> List[str]:
        return VIDEO_EXTENSIONS

    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """Extract a frame at ~10% of the video duration and save as JPEG thumbnail."""
        output_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(output_path):
            return output_path

        duration = self._get_duration(image_path)
        seek_time = max(duration * 0.1, 0.0) if duration else 2.0

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(seek_time),
                    "-i", image_path,
                    "-frames:v", "1",
                    "-vf", f"scale={self.thumbnail_size}:{self.thumbnail_size}:"
                           f"force_original_aspect_ratio=decrease",
                    "-q:v", "5",
                    output_path,
                ],
                capture_output=True, check=True, timeout=30,
            )
            return output_path if os.path.exists(output_path) else None
        except subprocess.SubprocessError as e:
            logger.error("ffmpeg thumbnail failed for %s: %s", image_path, e)
            return None

    def process_view_image(self, image_path: str, md5_hash: str) -> Optional[str]:
        """Return a poster frame for the brief moment before mpv starts rendering."""
        output_path = self.get_view_image_path(md5_hash)
        if os.path.exists(output_path):
            return output_path

        duration = self._get_duration(image_path)
        seek_time = max(duration * 0.1, 0.0) if duration else 2.0

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(seek_time),
                    "-i", image_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    output_path,
                ],
                capture_output=True, check=True, timeout=30,
            )
            return output_path if os.path.exists(output_path) else None
        except subprocess.SubprocessError as e:
            logger.error("ffmpeg view image failed for %s: %s", image_path, e)
            return None

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
