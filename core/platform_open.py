# core/platform_open.py
"""Platform-agnostic helpers for opening files and directories with their default application."""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def open_with_default_app(path: str) -> bool:
    """Open *path* with the OS default application. Returns True on success."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError as e:
        logger.warning("Failed to open %s with default app: %s", path, e)
        return False


def open_containing_folder(path: str) -> bool:
    """Open the folder that contains *path* (or *path* itself if it is a directory).

    On macOS the file is revealed in Finder (``open -R``).
    On Windows the file is selected in Explorer (``explorer /select,``).
    On Linux the parent directory is opened with ``xdg-open``.
    """
    p = Path(path)
    if p.is_dir():
        target = p
    else:
        target = p.parent

    try:
        if sys.platform == "darwin":
            if p.is_dir():
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["open", "-R", str(p)])
        elif sys.platform == "win32":
            if p.is_dir():
                subprocess.Popen(["explorer", str(target)])
            else:
                subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except OSError as e:
        logger.warning("Failed to open folder for %s: %s", path, e)
        return False
