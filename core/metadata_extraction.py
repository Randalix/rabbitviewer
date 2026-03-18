"""EXIF metadata extraction via plugins and exiftool.

Qt-free, database-free. Extracts metadata dicts that can be stored by the
database layer.
"""

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from plugins.base_plugin import plugin_registry, sidecar_path_for
from plugins.exiftool_process import ExifToolProcess

logger = logging.getLogger(__name__)

_fallback_exiftool_local = threading.local()


def _get_fallback_exiftool() -> ExifToolProcess:
    if not hasattr(_fallback_exiftool_local, "proc"):
        _fallback_exiftool_local.proc = ExifToolProcess()
    return _fallback_exiftool_local.proc


def _metadata_defaults() -> Dict[str, Any]:
    return {
        'rating': 0, 'file_size': 0, 'width': 0, 'height': 0,
        'camera_make': None, 'camera_model': None, 'lens_model': None,
        'focal_length': None, 'aperture': None, 'shutter_speed': None,
        'iso': None, 'date_taken': None, 'orientation': 1,
        'color_space': None, 'thumbnail_path': None, 'view_image_path': None,
        'exif_data': {}
    }


def extract_metadata_from_file(file_path: str, use_plugin: bool = True,
                               file_size: Optional[int] = None) -> Dict[str, Any]:
    """Extract all metadata from a file using plugins or exiftool.

    Returns a metadata dict ready for storage. No database access.
    """
    # --- Plugin Override ---
    _, ext = os.path.splitext(file_path)
    plugin = plugin_registry.get_plugin_for_format(ext)
    if use_plugin and plugin and hasattr(plugin, 'extract_metadata'):
        try:
            plugin_meta = plugin.extract_metadata(file_path)
            if plugin_meta is not None:
                logger.debug(
                    f"Using fast metadata extractor from plugin "
                    f"'{plugin.__class__.__name__}' for {os.path.basename(file_path)}"
                )
                metadata = _metadata_defaults()
                metadata.update(plugin_meta)
                if file_size is not None:
                    metadata['file_size'] = file_size
                else:
                    try:
                        metadata['file_size'] = os.path.getsize(file_path)  # disk-io: file size fallback
                    except OSError:
                        pass  # why: file may not exist; file_size defaults to 0
                return metadata
        except Exception as e:  # why: plugins are user-supplied
            logger.error(
                f"Plugin extractor '{plugin.__class__.__name__}' failed "
                f"for {file_path}: {e}. Falling back to default."
            )

    # --- Default Exiftool Fallback ---
    metadata = _metadata_defaults()

    try:
        metadata['file_size'] = file_size if file_size is not None else os.path.getsize(file_path)  # disk-io: file size

        raw = _get_fallback_exiftool().execute(
            ['-json', '-d', '%s', '-all', '-XMP:Rating', file_path]
        )
        exif_data = json.loads(raw)
        if exif_data and len(exif_data) > 0:
            data = exif_data[0]

            # Rating (prefer XMP:Rating)
            if 'XMP:Rating' in data:
                try:
                    metadata['rating'] = int(float(data['XMP:Rating']))
                except (ValueError, TypeError):
                    pass
            elif 'Rating' in data:
                try:
                    metadata['rating'] = int(float(data['Rating']))
                except (ValueError, TypeError):
                    pass

            # Image dimensions
            if 'ImageWidth' in data:
                try:
                    metadata['width'] = int(data['ImageWidth'])
                except (ValueError, TypeError):
                    pass
            if 'ImageHeight' in data:
                try:
                    metadata['height'] = int(data['ImageHeight'])
                except (ValueError, TypeError):
                    pass

            # Camera information
            metadata['camera_make'] = data.get('Make')
            metadata['camera_model'] = data.get('Model')
            metadata['lens_model'] = data.get('LensModel')

            # Capture parameters
            if 'FocalLength' in data:
                try:
                    focal_str = str(data['FocalLength']).replace('mm', '').strip()
                    metadata['focal_length'] = float(focal_str)
                except (ValueError, TypeError):
                    pass

            if 'FNumber' in data:
                try:
                    metadata['aperture'] = float(data['FNumber'])
                except (ValueError, TypeError):
                    pass

            metadata['shutter_speed'] = data.get('ShutterSpeed')

            if 'ISO' in data:
                try:
                    metadata['iso'] = int(data['ISO'])
                except (ValueError, TypeError):
                    pass

            # Date taken (epoch seconds via -d %s)
            for date_field in ['DateTimeOriginal', 'CreateDate', 'DateTime']:
                if date_field in data:
                    try:
                        metadata['date_taken'] = float(data[date_field])
                    except (ValueError, TypeError):
                        pass
                    break

            # Orientation
            if 'Orientation' in data:
                try:
                    metadata['orientation'] = int(data['Orientation'])
                except (ValueError, TypeError):
                    pass

            # Color space
            metadata['color_space'] = data.get('ColorSpace')

            # Keywords/tags from XMP:Subject and IPTC:Keywords
            keywords = set()
            for field in ('Subject', 'Keywords'):
                val = data.get(field)
                if isinstance(val, list):
                    keywords.update(str(v) for v in val if v)
                elif isinstance(val, str) and val:
                    keywords.add(val)
            if keywords:
                metadata['_keywords'] = list(keywords)

            # Full EXIF data
            metadata['exif_data'] = data

    except (TimeoutError, RuntimeError, json.JSONDecodeError, ValueError, FileNotFoundError) as e:
        logger.debug(f"Error extracting metadata from {file_path}: {e}")

    # Sidecar override: if FILENAME.ext.xmp exists, its rating/tags take precedence.
    xmp = sidecar_path_for(file_path)
    if os.path.exists(xmp):  # disk-io: sidecar check
        try:
            sc_raw = _get_fallback_exiftool().execute(
                ['-json', '-XMP:Rating', '-XMP:Subject', xmp]
            )
            sc_data = json.loads(sc_raw)
            if sc_data and len(sc_data) > 0:
                sc = sc_data[0]
                if 'XMP:Rating' in sc:
                    try:
                        metadata['rating'] = int(float(sc['XMP:Rating']))
                    except (ValueError, TypeError):
                        pass
                elif 'Rating' in sc:
                    try:
                        metadata['rating'] = int(float(sc['Rating']))
                    except (ValueError, TypeError):
                        pass
                subject = sc.get('Subject')
                if subject is not None:
                    keywords = set()
                    if isinstance(subject, list):
                        keywords.update(str(v) for v in subject if v)
                    elif isinstance(subject, str) and subject:
                        keywords.add(subject)
                    metadata['_keywords'] = list(keywords)
        except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            logger.debug(f"Error reading sidecar {xmp}: {e}")

    return metadata


def extract_fast_metadata_fields(file_path: str,
                                  stat_result: Optional[os.stat_result] = None,
                                  ) -> Optional[Dict[str, Any]]:
    """Plugin binary scan for orientation/rating/file_size only.

    Returns a dict with 'orientation', 'rating', 'file_size', 'mtime',
    'mtime_ns', 'birthtime', or None if no plugin is available.
    """
    _, ext = os.path.splitext(file_path)
    plugin = plugin_registry.get_plugin_for_format(ext)
    if not plugin or not hasattr(plugin, 'extract_metadata'):
        return None
    try:
        plugin_meta = plugin.extract_metadata(file_path)
    except Exception as e:  # why: plugins are user-supplied
        logger.debug(f"Fast metadata extraction failed for {file_path}: {e}")
        return None
    if plugin_meta is None:
        return None

    try:
        st = stat_result or os.stat(file_path)  # disk-io: fast metadata
    except OSError:
        return None

    return {
        'orientation': plugin_meta.get('orientation', 1),
        'rating': plugin_meta.get('rating', 0),
        'file_size': st.st_size,
        'mtime': st.st_mtime,
        'mtime_ns': st.st_mtime_ns,
        'birthtime': getattr(st, 'st_birthtime', None),
    }
