"""OCIO color management — assignment resolution and image transform.

Optional: gracefully disabled when PyOpenColorIO is not installed.

Usage
-----
Call ``ocio_manager.init(db)`` once at startup (ThumbnailService.__init__).
Then call ``ocio_manager.get_assignment(file_path)`` and, if an assignment
is returned, ``ocio_manager.apply(pil_image, assignment)`` to transform it.
"""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.db.ocio_table import OcioAssignment
    from core.metadata_database import MetadataDatabase

logger = logging.getLogger(__name__)

try:
    import PyOpenColorIO as ocio  # type: ignore[import-untyped]
    _OCIO_AVAILABLE = True
except ImportError:
    _OCIO_AVAILABLE = False
    logger.info("PyOpenColorIO not installed — OCIO color management disabled")


class OcioManager:
    """Singleton that resolves OCIO assignments and applies transforms."""

    def __init__(self) -> None:
        self._db: Optional["MetadataDatabase"] = None
        # Cache loaded OCIO configs keyed by config_path to avoid re-parsing
        self._config_cache: dict = {}

    def init(self, db: "MetadataDatabase") -> None:
        """Attach the database.  Call once at startup before any lookups."""
        self._db = db

    @property
    def available(self) -> bool:
        return _OCIO_AVAILABLE

    def get_assignment(self, file_path: str) -> Optional["OcioAssignment"]:
        """Return the effective OCIO assignment for *file_path*, or None."""
        if self._db is None or not _OCIO_AVAILABLE:
            return None
        return self._db.ocio.get_for_file(file_path)

    def _load_config(self, config_path: str):
        """Return a cached PyOpenColorIO.Config, or None on error."""
        if config_path in self._config_cache:
            return self._config_cache[config_path]
        try:
            cfg = ocio.Config.CreateFromFile(config_path)
            self._config_cache[config_path] = cfg
            logger.info("Loaded OCIO config: %s", config_path)
            return cfg
        except Exception as e:
            logger.error("Failed to load OCIO config %s: %s", config_path, e)
            self._config_cache[config_path] = None
            return None

    def apply(self, image, assignment: "OcioAssignment"):
        """Apply the OCIO transform from *assignment* to a PIL Image.

        Returns the transformed image, or the original on any error.
        Requires PyOpenColorIO to be installed.
        """
        if not _OCIO_AVAILABLE:
            return image

        try:
            import numpy as np
            from PIL import Image

            cfg = self._load_config(assignment.config_path)
            if cfg is None:
                return image

            display = assignment.display or cfg.getDefaultDisplay()
            view = assignment.view or cfg.getDefaultView(display)

            # Use DisplayViewTransform to map input space → display output
            xform = ocio.DisplayViewTransform(
                src=assignment.input_space,
                display=display,
                view=view,
            )
            proc = cfg.getProcessor(xform)
            cpu = proc.getDefaultCPUProcessor()

            # Ensure RGB
            if image.mode != "RGB":
                image = image.convert("RGB")

            arr = np.array(image, dtype=np.float32) / 255.0
            cpu.applyRGB(arr)
            arr = (arr.clip(0.0, 1.0) * 255.0).astype(np.uint8)
            return Image.fromarray(arr, "RGB")

        except Exception as e:
            logger.warning("OCIO transform failed: %s", e)
            return image

    def apply_to_float(self, arr, assignment: "OcioAssignment"):
        """Apply OCIO transform to a float32 HxWx3 array in scene-linear space.

        For HDR source data (EXR, HDR) where values are not pre-normalized to
        [0, 1].  Returns a float array clipped to [0, 1], or the original array
        on any error.
        """
        if not _OCIO_AVAILABLE:
            return arr
        try:
            import numpy as np
            cfg = self._load_config(assignment.config_path)
            if cfg is None:
                return arr
            display = assignment.display or cfg.getDefaultDisplay()
            view = assignment.view or cfg.getDefaultView(display)
            xform = ocio.DisplayViewTransform(
                src=assignment.input_space,
                display=display,
                view=view,
            )
            proc = cfg.getProcessor(xform)
            cpu = proc.getDefaultCPUProcessor()
            result = np.array(arr, dtype=np.float32)
            cpu.applyRGB(result)
            return result.clip(0.0, 1.0)
        except Exception as e:
            logger.warning("OCIO float transform failed: %s", e)
            return arr

    def invalidate_config_cache(self, config_path: Optional[str] = None) -> None:
        """Drop cached OCIO config(s) so they are reloaded on next use."""
        if config_path is None:
            self._config_cache.clear()
        else:
            self._config_cache.pop(config_path, None)


# Module-level singleton
ocio_manager = OcioManager()
