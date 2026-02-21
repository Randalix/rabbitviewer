import io
import logging
import os
import struct
from typing import List, Optional, Union, cast

from PIL import Image

from .base_plugin import BasePlugin
from .exiftool_process import is_exiftool_available

logger = logging.getLogger(__name__)


class RawPlugin(BasePlugin):
    """Plugin for common RAW formats (NEF, ARW, DNG, RAF, ORF, RW2, PEF, etc.) via exiftool."""

    def is_available(self) -> bool:
        return is_exiftool_available()

    def get_supported_formats(self) -> List[str]:
        return [
            ".nef",   # Nikon
            ".nrw",   # Nikon compact RAW
            ".arw",   # Sony
            ".sr2",   # Sony
            ".srf",   # Sony
            ".dng",   # Adobe / universal
            ".raf",   # Fujifilm
            ".orf",   # Olympus / OM System
            ".rw2",   # Panasonic
            ".pef",   # Pentax
            ".srw",   # Samsung
            ".mrw",   # Minolta
            ".rwl",   # Leica
            ".3fr",   # Hasselblad
            ".fff",   # Hasselblad
            ".mef",   # Mamiya
            ".mos",   # Mamiya
            ".iiq",   # Phase One
            ".cap",   # Phase One
            ".eip",   # Phase One
            ".cr2",   # Canon (legacy; CR3 handled by CR3Plugin)
        ]

    # ------------------------------------------------------------------
    # Orientation
    # ------------------------------------------------------------------

    def _get_orientation(self, image_path: str) -> int:
        """Extract EXIF Orientation via a fast binary scan of the file header."""
        try:
            with open(image_path, "rb") as f:
                buf = f.read(256 * 1024)
            return self._scan_exif_orientation(buf)
        except (IOError, struct.error) as e:
            logger.warning("Could not read orientation from %s: %s", image_path, e)
        return 1

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_jpg_from_raw_to_memory(self, image_path: str) -> Optional[bytes]:
        et = self._get_exiftool()
        for tag in ("-JpgFromRaw", "-PreviewImage"):
            try:
                data = et.execute([tag, "-b", image_path])
                if data:
                    return data
            except (RuntimeError, TimeoutError) as e:
                logger.warning("Failed to extract %s from %s: %s", tag, image_path, e)
        logger.warning("No embedded JPEG found for %s", image_path)
        return None

    def _extract_thumbnail_to_memory(self, image_path: str) -> Optional[bytes]:
        try:
            data = self._get_exiftool().execute(["-ThumbnailImage", "-b", image_path])
            if data:
                return data
        except (RuntimeError, TimeoutError) as e:
            logger.warning("Failed to extract ThumbnailImage from %s: %s", image_path, e)
        return None

    # ------------------------------------------------------------------
    # BasePlugin interface
    # ------------------------------------------------------------------

    def generate_view_image(self, image_path: str, image_source: Union[str, bytes],
                            orientation: int, output_path: str) -> bool:
        if not image_source:
            logger.error("Image bytes are empty for %s.", image_path)
            return False
        try:
            img = Image.open(io.BytesIO(cast(bytes, image_source)))
            img = self._apply_orientation(img, orientation)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, "JPEG", quality=85)
            return True
        except (OSError, ValueError) as e:
            logger.error("Error generating view image for %s: %s", image_path, e)
            return False

    def generate_thumbnail(self, image_path: str, image_source: Optional[Union[str, bytes]],
                           orientation: int, output_path: str) -> bool:
        if not image_source:
            return False
        try:
            img = Image.open(io.BytesIO(cast(bytes, image_source)))
            img = self._apply_orientation(img, orientation)
            if img.width > self.thumbnail_size or img.height > self.thumbnail_size:
                img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, "JPEG", quality=85)
            return True
        except (OSError, ValueError) as e:
            logger.error("Error generating thumbnail for %s: %s", image_path, e)
            return False

    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        thumbnail_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(thumbnail_path):
            return thumbnail_path
        try:
            orientation = (self._scan_exif_orientation(prefetch_buffer)
                           if prefetch_buffer is not None
                           else self._get_orientation(image_path))
            thumbnail_bytes = self._extract_thumbnail_to_memory(image_path)

            if self.generate_thumbnail(image_path, thumbnail_bytes, orientation, thumbnail_path):
                return thumbnail_path

            # Fall back to generating from the full preview image.
            logger.debug("Generating thumbnail from preview for %s", image_path)
            view_path = self.process_view_image(image_path, md5_hash)
            if not view_path or not os.path.exists(view_path):
                logger.error("Failed to obtain view image for thumbnail fallback: %s", image_path)
                return None
            with Image.open(view_path) as img:
                img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
                os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                img.save(thumbnail_path, "JPEG", quality=85)
            return thumbnail_path
        except (OSError, ValueError, struct.error) as e:
            logger.error("Unexpected error in process_thumbnail for %s: %s", image_path, e, exc_info=True)
            return None

    def process_view_image(self, image_path: str, md5_hash: str) -> Optional[str]:
        view_path = self.get_view_image_path(md5_hash)
        if os.path.exists(view_path):
            return view_path
        try:
            orientation = self._get_orientation(image_path)
            image_bytes = self._extract_jpg_from_raw_to_memory(image_path)
            if not image_bytes:
                logger.error("No embedded JPEG to build view image for %s", image_path)
                return None
            if self.generate_view_image(image_path, image_bytes, orientation, view_path):
                return view_path
            return None
        except (OSError, ValueError) as e:
            logger.error("Unexpected error in process_view_image for %s: %s", image_path, e, exc_info=True)
            return None

