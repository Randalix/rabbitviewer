"""Display-time OCIO transform helper (GUI thread only).

Converts QImage → PIL → OCIO → PIL → QImage.
Disabled gracefully when PyOpenColorIO is not installed.
"""

import logging

logger = logging.getLogger(__name__)


def apply_ocio_to_qimage(image, file_path: str):
    """Apply the OCIO assignment for *file_path* to *image* (QImage).

    Returns the transformed QImage, or the original if no assignment exists
    or PyOpenColorIO is unavailable.
    """
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
