import os
from datetime import datetime
from typing import List

from .content_provider import ContentProvider, Section


def _has(meta: dict, key: str) -> bool:
    """True if key exists and is not None."""
    return meta.get(key) is not None


class MetadataProvider(ContentProvider):
    """Formats cached EXIF/file metadata into collapsible sections."""

    def __init__(self, metadata_cache):
        self._cache = metadata_cache

    @property
    def provider_name(self) -> str:
        return "Metadata"

    def get_sections(self, image_path: str) -> List[Section]:
        meta = self._cache.get(image_path)
        if not meta:
            return [Section("Status", [("", "No metadata cached")])]

        sections = []

        # File info
        file_rows = [("Filename", os.path.basename(image_path))]
        w, h = meta.get("width"), meta.get("height")
        if w and h:
            file_rows.append(("Dimensions", f"{w} x {h}"))
        if meta.get("file_size"):
            size_mb = meta["file_size"] / (1024 * 1024)
            file_rows.append(("File Size", f"{size_mb:.1f} MB"))
        rating = meta.get("rating")
        if rating:
            file_rows.append(("Rating", "\u2605" * int(rating)))
        sections.append(Section("File", file_rows))

        # Camera info
        cam_rows = []
        for key, label in [
            ("camera_make", "Make"),
            ("camera_model", "Model"),
            ("lens_model", "Lens"),
        ]:
            if _has(meta, key):
                cam_rows.append((label, str(meta[key])))
        if cam_rows:
            sections.append(Section("Camera", cam_rows))

        # Exposure info
        exp_rows = []
        if _has(meta, "focal_length"):
            exp_rows.append(("Focal Length", f"{meta['focal_length']}mm"))
        if _has(meta, "aperture"):
            exp_rows.append(("Aperture", f"f/{meta['aperture']}"))
        if _has(meta, "shutter_speed"):
            exp_rows.append(("Shutter Speed", str(meta["shutter_speed"])))
        if _has(meta, "iso"):
            exp_rows.append(("ISO", str(meta["iso"])))
        if _has(meta, "date_taken"):
            try:
                dt_str = datetime.fromtimestamp(float(meta["date_taken"])).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, OSError):
                dt_str = str(meta["date_taken"])
            exp_rows.append(("Date", dt_str))
        if exp_rows:
            sections.append(Section("Exposure", exp_rows))

        return sections
