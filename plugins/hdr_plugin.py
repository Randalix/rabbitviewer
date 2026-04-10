import logging
import os
import re
from typing import Optional, List, Union

logger = logging.getLogger(__name__)

from PIL import Image

try:
    import numpy as np
    _numpy_available = True
except ImportError:
    _numpy_available = False

from .base_plugin import BasePlugin


class HDRPlugin(BasePlugin):

    def is_available(self) -> bool:
        if not _numpy_available:
            logger.warning("HDRPlugin unavailable: numpy not installed.")
        return _numpy_available

    def get_supported_formats(self) -> List[str]:
        return ['.hdr', '.pic']

    def _read_hdr_as_pil(self, path: str, file_path: Optional[str] = None) -> Optional[Image.Image]:
        """
        Decodes a Radiance RGBE (.hdr/.pic) file into a PIL Image.

        If an OCIO assignment exists for *file_path* (defaults to *path*),
        applies the DisplayViewTransform to the raw float data.  Otherwise
        falls back to Reinhard tone-mapping.
        """
        if not _numpy_available:
            return None
        try:
            with open(path, 'rb') as f:  # disk-io: source image read
                buf = f.read()

            # --- Header ---
            pos = 0
            while pos < len(buf):
                end = buf.find(b'\n', pos)
                if end == -1:
                    break
                line = buf[pos:end]
                pos = end + 1
                if line == b'':
                    break  # blank line ends header

            # Resolution line: e.g. "-Y 512 +X 512"
            res_end = buf.find(b'\n', pos)
            res_line = buf[pos:res_end].decode('ascii').strip()
            pos = res_end + 1

            m = re.match(r'([+-][YX])\s+(\d+)\s+[+-][YX]\s+(\d+)', res_line)
            if not m:
                raise ValueError(f"Unrecognised HDR resolution line: {res_line!r}")
            if 'Y' in m.group(1):
                height, width = int(m.group(2)), int(m.group(3))
            else:
                width, height = int(m.group(2)), int(m.group(3))

            # --- Pixel data ---
            img_data = np.zeros((height, width, 3), dtype=np.float32)

            for y in range(height):
                # Detect encoding: new RLE starts with (2, 2, w_hi, w_lo)
                if (width >= 8 and width <= 0x7FFF
                        and buf[pos] == 2 and buf[pos + 1] == 2
                        and (buf[pos + 2] & 0x80) == 0):
                    # New RLE — four channels encoded separately
                    pos += 4
                    channels = []
                    for _ in range(4):
                        ch = []
                        while len(ch) < width:
                            code = buf[pos]; pos += 1
                            if code > 128:
                                ch.extend([buf[pos]] * (code - 128)); pos += 1
                            else:
                                ch.extend(buf[pos:pos + code]); pos += code
                        channels.append(np.array(ch[:width], dtype=np.uint8))
                    rgbe = np.stack(channels, axis=1)  # (W, 4)
                else:
                    # Old format — flat RGBE, 4 bytes per pixel
                    rgbe = np.frombuffer(buf[pos:pos + width * 4],
                                         dtype=np.uint8).reshape(width, 4).copy()
                    pos += width * 4

                # RGBE → float: value = mantissa * 2^(exponent - 128 - 8)
                mask = rgbe[:, 3] != 0
                if mask.any():
                    exp = rgbe[mask, 3].astype(np.float32) - 128 - 8
                    scale = np.power(2.0, exp)
                    img_data[y, mask, 0] = rgbe[mask, 0] * scale
                    img_data[y, mask, 1] = rgbe[mask, 1] * scale
                    img_data[y, mask, 2] = rgbe[mask, 2] * scale

            # Apply OCIO if assigned, otherwise fall back to Reinhard tone-mapping.
            ocio_path = file_path or path
            ocio_applied = False
            if ocio_path:
                from core.ocio_manager import ocio_manager
                assignment = ocio_manager.get_assignment(ocio_path)
                if assignment is not None:
                    img_data = ocio_manager.apply_to_float(img_data, assignment)
                    img_data = (img_data * 255).astype(np.uint8)
                    ocio_applied = True
            if not ocio_applied:
                img_data = img_data / (1 + img_data)
                img_data = np.clip(img_data * 255, 0, 255).astype(np.uint8)

            return Image.fromarray(img_data, 'RGB')
        except (OSError, ValueError, KeyError) as e:
            logger.error(f"Error reading HDR file {path}: {e}")
            return None

    def generate_view_image(self, image_path: str, image_source: Union[str, bytes], orientation: int, output_path: str) -> bool:
        img = self._read_hdr_as_pil(image_path, file_path=image_path)
        if not img:
            return False
        try:
            img = self._apply_orientation(img, orientation)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, 'JPEG', quality=95)
            logger.debug(f"Generated view image: {output_path}")
            return True
        except (OSError, ValueError) as e:
            logger.error(f"Error generating view image for {image_path}: {e}")
            return False

    def generate_thumbnail(self, image_path: str, image_source: Optional[Union[str, bytes]], orientation: int, output_path: str) -> bool:
        img = self._read_hdr_as_pil(image_path, file_path=image_path)
        if not img:
            return False
        try:
            img = self._apply_orientation(img, orientation)
            img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            img.save(output_path, 'JPEG', quality=85)
            logger.debug(f"Generated thumbnail: {output_path}")
            return True
        except (OSError, ValueError) as e:
            logger.error(f"Error generating thumbnail for {image_path}: {e}")
            return False

    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        thumbnail_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(thumbnail_path):  # disk-io: local cache file check
            return thumbnail_path

        try:
            buf = prefetch_buffer or open(image_path, 'rb').read(256 * 1024)  # disk-io: scan EXIF orientation header
            orientation = self._scan_exif_orientation(buf)
        except OSError as e:
            logger.warning(f"Could not read {image_path} for orientation scan: {e}")
            orientation = 1

        if self.generate_thumbnail(image_path, image_source=image_path, orientation=orientation, output_path=thumbnail_path):
            return thumbnail_path
        return None

    def process_view_image(self, image_path: str, md5_hash: str,
                           cancel_event=None,
                           prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        view_image_path = self.get_view_image_path(md5_hash)
        if os.path.exists(view_image_path):  # disk-io: local cache file check
            return view_image_path

        try:
            buf = prefetch_buffer or open(image_path, 'rb').read(256 * 1024)  # disk-io: scan EXIF orientation header
            orientation = self._scan_exif_orientation(buf)
        except OSError as e:
            logger.warning(f"Could not read {image_path} for orientation scan: {e}")
            orientation = 1

        if cancel_event and cancel_event.is_set():
            return None

        if self.generate_view_image(image_path, image_source=image_path, orientation=orientation, output_path=view_image_path):
            return view_image_path
        return None
