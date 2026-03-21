"""Sort all displayed images in colour-wheel order.

Computes mean HSV from each cached thumbnail.  Colourful images are ordered
by hue: red → orange → yellow → green → cyan → blue → magenta → red.
Desaturated images (grey, black-and-white, fog) are sorted by brightness and
placed at the end.  No reference image is required.
"""
import colorsys
import math
from pathlib import Path

# Images whose mean saturation falls below this threshold are treated as neutral.
_SAT_THRESHOLD = 0.12


def _mean_hsv(thumbnail_path):
    """Return (mean_h, mean_s, mean_v) for the thumbnail, or None on failure.

    Hue is computed as a circular mean weighted by per-pixel saturation so
    that near-grey pixels don't distort the dominant hue.
    """
    try:
        from PIL import Image

        img = Image.open(thumbnail_path).convert("RGB").resize((16, 16), Image.Resampling.LANCZOS)  # disk-io: local thumbnail cache
        pixels = list(img.getdata())
        n = len(pixels)

        sin_sum = cos_sum = s_sum = v_sum = 0.0
        for r, g, b in pixels:
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            angle = 2.0 * math.pi * h
            sin_sum += math.sin(angle) * s
            cos_sum += math.cos(angle) * s
            s_sum += s
            v_sum += v

        mean_s = s_sum / n
        mean_v = v_sum / n
        w = s_sum if s_sum > 0.01 else 0.01
        mean_h = math.atan2(sin_sum / w, cos_sum / w) / (2.0 * math.pi) % 1.0

        return (mean_h, mean_s, mean_v)
    except Exception:
        return None


def run_script(api):
    """Reorder displayed images in colour-wheel hue order."""
    all_images = api.get_all_images()
    if not all_images:
        return

    metadata = api.get_metadata_batch(all_images)

    colorful = []   # (hue, path)
    neutral = []    # (brightness, path)
    no_thumb = []

    for path in all_images:
        thumb = (metadata.get(path) or {}).get("thumbnail_path")
        if thumb and Path(thumb).exists():
            hsv = _mean_hsv(thumb)
            if hsv is not None:
                h, s, v = hsv
                if s >= _SAT_THRESHOLD:
                    colorful.append((h, path))
                else:
                    neutral.append((v, path))
                continue
        no_thumb.append(path)

    if not colorful and not neutral:
        api.show_message(
            "No thumbnails available yet — please wait for indexing to finish."
        )
        return

    colorful.sort(key=lambda x: x[0])
    neutral.sort(key=lambda x: x[0])   # dark → light within neutrals

    sorted_paths = (
        [p for _, p in colorful]
        + [p for _, p in neutral]
        + no_thumb
    )

    if sorted_paths != all_images:
        api.set_image_order(sorted_paths)
