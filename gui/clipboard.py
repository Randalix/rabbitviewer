"""Clipboard operations for copying file paths, files, and image pixels."""

import os
import logging
from typing import List

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QUrl, QMimeData
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

_JPEG_EXTENSIONS = frozenset({'.jpg', '.jpeg'})


def copy_paths_as_text(paths: List[str]) -> int:
    """Copy file paths as newline-separated text. Returns count copied."""
    if not paths:
        return 0
    QApplication.clipboard().setText("\n".join(paths))
    return len(paths)


def copy_files_to_clipboard(paths: List[str]) -> int:
    """Copy files as URLs so they can be pasted in Finder/file browsers. Returns count."""
    if not paths:
        return 0
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    QApplication.clipboard().setMimeData(mime)
    return len(paths)


def copy_image_pixels(image: QImage | None, path: str | None) -> bool:
    """Copy QImage pixels to clipboard. Only for JPEG files. Returns success."""
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in _JPEG_EXTENSIONS:
        logger.info("copy_image_pixels: skipped non-JPEG file %s", path)
        return False
    if image is None or image.isNull():
        logger.warning("copy_image_pixels: no image loaded for %s", path)
        return False
    QApplication.clipboard().setImage(image)
    return True
