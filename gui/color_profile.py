"""Display color management via ICC monitor profiles.

Converts images from sRGB (the assumed color space of all cached images)
to the user's monitor ICC profile before display.  Disabled when no
profile is configured.
"""

import logging
from typing import Optional

from PySide6.QtGui import QColorSpace, QImage, QPixmap

logger = logging.getLogger(__name__)

_monitor_cs: Optional[QColorSpace] = None
_srgb_cs: Optional[QColorSpace] = None
_initialized = False


def _ensure_init() -> None:
    global _monitor_cs, _srgb_cs, _initialized
    if _initialized:
        return
    _initialized = True
    _srgb_cs = QColorSpace(QColorSpace.SRgb)

    from config.config_manager import ConfigManager
    cfg = ConfigManager()
    path = cfg.get("color_management.icc_profile_path", "")
    if not path:
        return

    try:
        with open(path, "rb") as f:  # disk-io: load monitor ICC profile
            data = f.read()
        cs = QColorSpace.fromIccProfile(data)
        if not cs.isValid():
            logger.warning("ICC profile loaded but invalid: %s", path)
            return
        _monitor_cs = cs
        logger.info("Color management enabled: %s", path)
    except FileNotFoundError:
        logger.warning("ICC profile not found: %s", path)
    except Exception:
        logger.warning("Failed to load ICC profile: %s", path, exc_info=True)


def apply_profile(image: QImage) -> QImage:
    """Convert *image* from sRGB to the monitor color space.

    Returns the image unchanged if no profile is configured or on error.
    """
    _ensure_init()
    if _monitor_cs is None or image.isNull():
        return image
    image.setColorSpace(_srgb_cs)
    return image.convertedToColorSpace(_monitor_cs)


def apply_profile_pixmap(image: QImage) -> QPixmap:
    """Color-convert *image* and return as QPixmap."""
    return QPixmap.fromImage(apply_profile(image))
