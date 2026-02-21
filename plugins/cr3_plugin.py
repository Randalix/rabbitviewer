import logging
import os
from PIL import Image
from typing import Optional, List, Union, cast

from .base_plugin import BasePlugin
from .exiftool_process import is_exiftool_available
import io
import struct

# Logger for this module
logger = logging.getLogger(__name__)

class CR3Plugin(BasePlugin):
    """Plugin for handling Canon CR3 RAW files using exiftool."""

    def is_available(self) -> bool:
        """Checks if exiftool is available in the system's PATH."""
        return is_exiftool_available()
    
    def get_supported_formats(self) -> List[str]:
        """Return list of supported file extensions."""
        return ['.cr3'] 
    
    def _get_orientation_from_buffer(self, buffer: bytes) -> int:
        """Extract EXIF Orientation from an already-read byte buffer (no file I/O)."""
        return self._scan_exif_orientation(buffer)

    def _get_orientation_from_cr3(self, cr3_path: str) -> int:
        """
        Reads the Orientation tag from the original CR3 file using a fast binary search.
        Returns the integer value of the Orientation tag, or 1 if not found/error.
        """
        try:
            with open(cr3_path, 'rb') as f:
                buffer = f.read(256 * 1024)
            return self._get_orientation_from_buffer(buffer)
        except (IOError, struct.error) as e:
            logger.warning(f"Could not read Orientation from {cr3_path} using fast parser: {e}")
            return 1

    # Canon's metadata UUID that contains the embedded thumbnail JPEG.
    _CANON_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')

    def _extract_thumbnail_from_buffer(self, buffer: bytes) -> Optional[bytes]:
        """
        Extract the thumbnail JPEG from the Canon uuid box in an already-read
        CR3 file buffer, with no additional file I/O or subprocess calls.

        CR3 (Canon's ISOBMFF-based RAW format) embeds the small thumbnail JPEG
        inside a proprietary uuid box rather than in the standard EXIF IFD1, so
        standard TIFF/IFD parsing does not find it.  The thumbnail is always in
        the first Canon uuid box (GUID 85c0b687...) which lives within the moov
        box near the start of the file — well within a 512 KB prefetch buffer.

        Returns the raw JPEG bytes on success, or None if the thumbnail was not
        found (caller should fall back to exiftool).
        """
        try:
            # Walk the top-level ISOBMFF boxes to find moov.
            pos = 0
            n = len(buffer)
            while pos + 8 <= n:
                box_size = struct.unpack_from('>I', buffer, pos)[0]
                box_type = buffer[pos + 4: pos + 8]
                if box_size < 8:
                    break
                if box_type == b'moov':
                    moov_end = min(pos + box_size, n)
                    # Walk moov children looking for the Canon uuid.
                    inner = pos + 8
                    while inner + 24 <= moov_end:
                        isz  = struct.unpack_from('>I', buffer, inner)[0]
                        ityp = buffer[inner + 4: inner + 8]
                        if isz < 8:
                            break
                        if ityp == b'uuid' and buffer[inner + 8: inner + 24] == self._CANON_UUID:
                            # Canon uuid found — the thumbnail JPEG is the first
                            # JPEG whose fourth byte is a standard header marker
                            # (DQT 0xDB or APPn 0xE0–0xEF).  Canon proprietary
                            # data in the same block may start with 0xFF 0xD8 0xFF
                            # followed by an SOF marker (e.g. 0xC1); those are
                            # skipped.
                            content_start = inner + 24   # skip 8-byte header + 16-byte UUID
                            content_end   = min(inner + isz, n)
                            search_pos = content_start
                            soi = -1
                            while search_pos < content_end - 3:
                                p = buffer.find(b'\xff\xd8\xff', search_pos, content_end)
                                if p == -1:
                                    break
                                fourth = buffer[p + 3]
                                if fourth == 0xDB or 0xE0 <= fourth <= 0xEF:
                                    soi = p
                                    break
                                search_pos = p + 3
                            if soi == -1:
                                return None
                            eoi = buffer.find(b'\xff\xd9', soi + 2, content_end)
                            if eoi == -1:
                                logger.debug("CR3 thumbnail EOI not in prefetch buffer; falling back to exiftool.")
                                return None
                            return buffer[soi: eoi + 2]
                        inner += isz
                    break   # moov processed; thumbnail not found
                pos += box_size
        except struct.error:
            pass
        return None

    def _extract_jpg_from_raw_to_memory(self, image_path: str) -> Optional[bytes]:
        """
        Extracts the embedded preview JPG into an in-memory bytes buffer via the
        persistent exiftool process. Tries JpgFromRaw first, falls back to PreviewImage.
        """
        et = self._get_exiftool()

        # First attempt: JpgFromRaw (typically highest quality)
        logger.debug(f"Attempting to extract JpgFromRaw from {os.path.basename(image_path)}...")
        try:
            data = et.execute(["-JpgFromRaw", "-b", image_path])
            if data:
                logger.debug("Successfully extracted JpgFromRaw.")
                return data
        except (RuntimeError, TimeoutError) as e:
            logger.warning(f"Failed to extract JpgFromRaw for {image_path}: {e}")

        # Fallback: PreviewImage
        logger.debug(f"JpgFromRaw empty/failed; falling back to PreviewImage for {os.path.basename(image_path)}.")
        try:
            data = et.execute(["-PreviewImage", "-b", image_path])
            if data:
                logger.debug("Successfully extracted PreviewImage.")
                return data
            logger.warning(f"PreviewImage also empty for {image_path}.")
            return None
        except (RuntimeError, TimeoutError) as e:
            logger.warning(f"Failed to extract PreviewImage for {image_path}: {e}")
            return None

    def _extract_thumbnail_to_memory(self, image_path: str) -> Optional[bytes]:
        """
        Extracts ThumbnailImage into an in-memory bytes buffer via the persistent
        exiftool process.
        """
        logger.debug(f"Extracting ThumbnailImage from {os.path.basename(image_path)}...")
        try:
            data = self._get_exiftool().execute(["-ThumbnailImage", "-b", image_path])
            if data:
                return data
            logger.warning(f"Exiftool returned no ThumbnailImage data for {image_path}.")
            return None
        except (RuntimeError, TimeoutError) as e:
            logger.warning(f"Failed to extract ThumbnailImage for {image_path}: {e}")
            return None

    def generate_view_image(self, image_path: str, image_source: Union[str, bytes], orientation: int, output_path: str) -> bool:
        """
        Convert image bytes to JPG for viewing and save to output_path.
        image_source must be bytes for CR3 (extracted JPG from raw).
        """
        if not image_source:
            logger.error(f"Image bytes are empty for {image_path}.")
            return False

        try:
            img = Image.open(io.BytesIO(cast(bytes, image_source)))
            img = self._apply_orientation(img, orientation)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, "JPEG", quality=85)
            logger.debug(f"Successfully processed and saved view image {output_path}")
            return True
        except (OSError, ValueError) as e:
            logger.error(f"Error processing view image from bytes for {image_path}: {e}")
            return False

    def generate_thumbnail(self, image_path: str, image_source: Optional[Union[str, bytes]], orientation: int, output_path: str) -> bool:
        """
        Generate thumbnail JPG from bytes and save to output_path.
        image_source must be bytes for CR3 (extracted JPG from raw).
        """
        if not image_source:
            return False

        try:
            img = Image.open(io.BytesIO(cast(bytes, image_source)))
            img = self._apply_orientation(img, orientation)

            # Resize to desired thumbnail_size if necessary
            if img.width > self.thumbnail_size or img.height > self.thumbnail_size:
                img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
                logger.debug(f"Resized extracted thumbnail for {image_path} to {img.width}x{img.height}")

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, "JPEG", quality=85)
            logger.debug(f"Successfully processed and saved thumbnail {output_path}")
            return True
        except (OSError, ValueError) as e:
            logger.error(f"Error processing thumbnail from bytes for {image_path}: {e}")
            return False

    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """
        Generates a thumbnail, prioritizing the embedded version and falling back
        to the view image if necessary.

        If *prefetch_buffer* is supplied (the first N bytes already read by the
        caller), orientation and the IFD1 thumbnail JPEG are extracted from it
        without any additional file I/O.  Falls back to exiftool only when the
        buffer doesn't contain the full thumbnail.
        """
        thumbnail_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(thumbnail_path):
            return thumbnail_path

        try:
            if prefetch_buffer is not None:
                orientation = self._get_orientation_from_buffer(prefetch_buffer)
                thumbnail_bytes = self._extract_thumbnail_from_buffer(prefetch_buffer)
                if thumbnail_bytes is None:
                    # Buffer didn't cover the full thumbnail; use exiftool as fallback.
                    thumbnail_bytes = self._extract_thumbnail_to_memory(image_path)
            else:
                orientation = self._get_orientation_from_cr3(image_path)
                thumbnail_bytes = self._extract_thumbnail_to_memory(image_path)

            # First attempt: use the extracted embedded thumbnail
            if self.generate_thumbnail(image_path, thumbnail_bytes, orientation, thumbnail_path):
                return thumbnail_path

            # If embedded thumbnail fails or is too small, generate from the main view image.
            logger.debug(f"Generating thumbnail for {image_path} from its main preview image.")
            view_image_path = self.process_view_image(image_path, md5_hash) # This extracts the high-quality JPG
            if not view_image_path or not os.path.exists(view_image_path):
                logger.error(f"Failed to create or find view image to generate thumbnail for {image_path}")
                return None

            # Now, create the thumbnail by resizing the high-quality view image
            with Image.open(view_image_path) as img:
                img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
                os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                img.save(thumbnail_path, "JPEG", quality=85)
                return thumbnail_path

        except (OSError, ValueError, struct.error) as e:
            logger.error(f"Unexpected error in process_thumbnail for {image_path}: {e}", exc_info=True)
            return None

    def process_view_image(self, image_path: str, md5_hash: str) -> Optional[str]:
        """
        Generates the full-resolution view image from the raw file's embedded JPG.
        """
        view_image_path = self.get_view_image_path(md5_hash)
        if os.path.exists(view_image_path):
            return view_image_path

        try:
            orientation = self._get_orientation_from_cr3(image_path)
            image_bytes = self._extract_jpg_from_raw_to_memory(image_path)

            if image_bytes:
                if self.generate_view_image(image_path, image_bytes, orientation, view_image_path):
                    return view_image_path
                else:
                    logger.error(f"Failed to convert temp preview to final view image for {image_path}")
                    return None
            else:
                logger.error(f"Failed to extract JpgFromRaw to generate view image for {image_path}")
                return None
        except (OSError, ValueError) as e:
            logger.error(f"Unexpected error in process_view_image for {image_path}: {e}", exc_info=True)
            return None

