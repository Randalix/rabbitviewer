import logging
from typing import Optional, List, Union
import os

logger = logging.getLogger(__name__)

from PIL import Image

try:
    import numpy as np
    import OpenEXR
    _exr_available = True
except ImportError:
    _exr_available = False

from .base_plugin import BasePlugin


class EXRPlugin(BasePlugin):
    def is_available(self) -> bool:
        if not _exr_available:
            logger.warning("EXRPlugin unavailable: openexr or numpy not installed. Try 'pip install openexr'")
        return _exr_available

    def get_supported_formats(self) -> List[str]:
        return ['.exr']

    def _read_exr_as_pil(self, source: Union[str, bytes], file_path: Optional[str] = None) -> Optional[Image.Image]:
        """
        Reads an EXR file and returns a PIL Image.

        If an OCIO assignment exists for *file_path*, applies the DisplayViewTransform
        to the raw float data.  Otherwise falls back to Reinhard tone-mapping.
        """
        if not _exr_available:
            return None
        try:
            f = OpenEXR.File(source)
            part = f.parts[0]
            channels = part.channels
            ch_names = list(channels.keys())

            # Resolve RGB data from however the channels are laid out.
            if 'RGBA' in ch_names:
                img_data = np.array(channels['RGBA'].pixels, dtype=np.float32)[:, :, :3]
            elif 'RGB' in ch_names:
                img_data = np.array(channels['RGB'].pixels, dtype=np.float32)
            elif all(c in ch_names for c in ('R', 'G', 'B')):
                r = np.array(channels['R'].pixels, dtype=np.float32)
                g = np.array(channels['G'].pixels, dtype=np.float32)
                b = np.array(channels['B'].pixels, dtype=np.float32)
                img_data = np.stack([r, g, b], axis=2)
            elif len(ch_names) >= 3:
                arrays = [np.array(channels[c].pixels, dtype=np.float32) for c in ch_names[:3]]
                img_data = np.stack(arrays, axis=2)
            elif len(ch_names) == 1:
                raw = np.array(channels[ch_names[0]].pixels, dtype=np.float32)
                img_data = raw[:, :, 0] if raw.ndim == 3 else raw
            else:
                logger.error(f"EXR has no recognizable RGB channels: {ch_names}")
                return None

            # Apply OCIO if assigned, otherwise fall back to Reinhard tone-mapping.
            ocio_applied = False
            if file_path:
                from core.ocio_manager import ocio_manager
                assignment = ocio_manager.get_assignment(file_path)
                if assignment is not None:
                    img_data = ocio_manager.apply_to_float(img_data, assignment)
                    img_data = (img_data * 255).astype(np.uint8)
                    ocio_applied = True
            if not ocio_applied:
                img_data = img_data / (1 + img_data)
                img_data = np.clip(img_data * 255, 0, 255).astype(np.uint8)

            img = Image.fromarray(img_data, 'L' if img_data.ndim == 2 else None)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            return img
        except (OSError, ValueError, KeyError) as e:
            logger.error(f"Error reading EXR file: {e}")
            return None

    def generate_view_image(self, image_path: str, image_source: Union[str, bytes], orientation: int, output_path: str) -> bool:
        """
        Creates a viewable JPEG from the source EXR by decoding, tone-mapping,
        and applying orientation, then saves it to the output path.
        """
        img = self._read_exr_as_pil(image_source, file_path=image_path)
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
        """
        Creates a thumbnail from the source EXR by decoding, tone-mapping,
        applying orientation, resizing, and saving to the output path.
        """
        source = image_source if image_source else image_path
        img = self._read_exr_as_pil(source, file_path=image_path)
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
            buf = prefetch_buffer or open(image_path, "rb").read(256 * 1024)  # disk-io: scan EXIF orientation header
            orientation = self._scan_exif_orientation(buf)
        except OSError as e:
            logger.warning(f"Could not read {image_path} to scan for orientation: {e}")
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
            buf = prefetch_buffer or open(image_path, "rb").read(256 * 1024)  # disk-io: scan EXIF orientation header
            orientation = self._scan_exif_orientation(buf)
        except OSError as e:
            logger.warning(f"Could not read {image_path} to scan for orientation: {e}")
            orientation = 1

        if cancel_event and cancel_event.is_set():
            return None

        if self.generate_view_image(image_path, image_source=image_path, orientation=orientation, output_path=view_image_path):
            return view_image_path
        return None
