import os
import logging
from typing import List

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QUrl, QMimeData
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

JPEG_EXTENSIONS = frozenset({'.jpg', '.jpeg'})


def copy_paths_as_text(paths: List[str]) -> int:
    if not paths:
        return 0
    QApplication.clipboard().setText("\n".join(paths))
    return len(paths)


def copy_files_to_clipboard(paths: List[str]) -> int:
    if not paths:
        return 0
    mime = QMimeData()
    # file:// URLs, not raw paths — compatible with Finder/file-browser paste
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    QApplication.clipboard().setMimeData(mime)
    return len(paths)


def copy_image_pixels(image: QImage | None, path: str | None) -> bool:
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in JPEG_EXTENSIONS:
        logger.info("copy_image_pixels: skipped non-JPEG file %s", path)
        return False
    if image is None or image.isNull():
        logger.warning("copy_image_pixels: no image loaded for %s", path)
        return False
    QApplication.clipboard().setImage(image)
    return True
