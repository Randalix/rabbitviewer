# core/file_ops.py
"""Sidecar-aware file operations (trash, remove).

All public functions accept ``List[str]`` of image paths to match existing
operation-registry signatures.  Sidecar resolution is handled internally.
"""
import logging
import os
from typing import Any, Dict, List

from core.priority import _xmp_sidecar_path

logger = logging.getLogger(__name__)


def resolve_sidecars(image_path: str) -> List[str]:
    """Return existing sidecar paths for *image_path*."""
    xmp = _xmp_sidecar_path(image_path)
    if os.path.exists(xmp):
        return [xmp]
    return []


def _get_send2trash():
    from send2trash import send2trash
    return send2trash


def trash_with_sidecars(file_paths: List[str]) -> Dict[str, Any]:
    """Move images and their sidecars to the system trash.

    Home-trash fallback on macOS when volume trash is unavailable.
    Sidecar failures are non-fatal — logged but don't affect image result.
    """
    _send2trash = _get_send2trash()

    succeeded, failed = 0, 0

    for path in file_paths:
        # Trash the image
        try:
            _send2trash(path)
            succeeded += 1
        except OSError as e:
            if "Directory not found" in str(e):
                home_trash = os.path.expanduser("~/.Trash")
                try:
                    os.makedirs(home_trash, exist_ok=True)
                    import shutil
                    shutil.move(path, home_trash)
                    succeeded += 1
                except Exception as fallback_e:  # why: shutil.move raises shutil.Error (not OSError) on cross-device failure
                    logger.warning(f"Home trash fallback also failed for {path}: {fallback_e}")
                    failed += 1
                    continue
            else:
                logger.warning(f"Failed to trash {path}: {e}")
                failed += 1
                continue
        except Exception as e:  # why: send2trash raises platform-specific exceptions beyond OSError
            logger.warning(f"Failed to trash {path}: {e}")
            failed += 1
            continue

        # Trash sidecars (non-fatal)
        for sidecar in resolve_sidecars(path):
            try:
                _send2trash(sidecar)
                logger.debug(f"Trashed sidecar: {sidecar}")
            except Exception as e:  # why: sidecar trash failure is non-fatal; continue with remaining files
                logger.warning(f"Failed to trash sidecar {sidecar}: {e}")

    logger.info(f"send2trash: {succeeded} trashed, {failed} failed out of {len(file_paths)}")
    return {"succeeded": succeeded, "failed": failed}


def remove_with_sidecars(file_paths: List[str]) -> None:
    """Remove images and their sidecars via ``os.remove()``.

    Sidecar failures are non-fatal.
    """
    for path in file_paths:
        # Remove sidecars first (before the image disappears from the filesystem)
        for sidecar in resolve_sidecars(path):
            try:
                os.remove(sidecar)
                logger.debug(f"Removed sidecar: {sidecar}")
            except OSError as e:
                logger.warning(f"Failed to remove sidecar {sidecar}: {e}")

        try:
            os.remove(path)
        except OSError as e:
            logger.warning(f"Failed to remove {path}: {e}")
