"""Display-time OCIO transform helper (GUI thread only).

Converts QImage → PIL → OCIO → PIL → QImage.
Disabled gracefully when PyOpenColorIO is not installed.

Note: HDR file types (.exr, .hdr, .pic) have OCIO applied during thumbnail/view
generation by their plugins (on raw float data).  Display-time OCIO is skipped
for these formats to avoid a double transform.
"""

import os
import logging

logger = logging.getLogger(__name__)

# Extensions handled at generation time by their plugins (EXRPlugin, HDRPlugin).
_PLUGIN_OCIO_EXTENSIONS = frozenset({'.exr', '.hdr', '.pic'})


def apply_ocio_to_qimage(image, file_path: str):
    """Apply the OCIO assignment for *file_path* to *image* (QImage).

    Returns the transformed QImage, or the original if no assignment exists,
    PyOpenColorIO is unavailable, or the file type is handled by its plugin.
    """
    if os.path.splitext(file_path)[1].lower() in _PLUGIN_OCIO_EXTENSIONS:
        return image

    from core.ocio_manager import ocio_manager
    if not ocio_manager.available:
        return image

    assignment = ocio_manager.get_assignment(file_path)
    if assignment is None:
        return image

    try:
        from PIL.ImageQt import fromqimage, toqimage

        pil = fromqimage(image).convert("RGB")
        transformed = ocio_manager.apply(pil, assignment)
        result = toqimage(transformed)
        return result
    except Exception as e:
        logger.warning("OCIO display transform failed for %s: %s", file_path, e)
        return image
