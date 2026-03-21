"""Sort displayed images by estimated visual quality, worst first.

Scores each image from its cached thumbnail using three pixel-level signals —
no model loading or inference required:

  - Laplacian variance  → sharpness (low = blurry)
  - Clipping ratio      → over/underexposure (high = clipped highlights or shadows)
  - RMS contrast        → flatness (low = washed-out, foggy, or featureless)

Each metric is min-max normalised across the batch and combined into a single
quality score.  Images without a cached thumbnail are placed at the end.
"""

# Relative weights for each normalised metric in the quality formula.
# Sharpness is the dominant signal; contrast is a softer, secondary cue
# (intentional silhouettes or high-key shots have naturally low contrast, so
# over-weighting it would penalise valid creative choices).
_W_SHARPNESS = 1.0
_W_CLIPPING  = 1.0
_W_CONTRAST  = 0.5


def _compute_metrics(arr, np):
    """Return (sharpness, clip_ratio, rms_contrast) for an (H, W, 3) float32 array."""
    # Perceptual luminance (ITU-R BT.601 weights)
    luma = arr[:, :, 0] * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114  # (H, W)

    # Laplacian variance — proxy for sharpness
    lap = (
        luma[1:-1, 1:-1] * -4
        + luma[:-2, 1:-1]
        + luma[2:,  1:-1]
        + luma[1:-1, :-2]
        + luma[1:-1, 2:]
    )
    sharpness = float(np.var(lap))

    # Clipping ratio — fraction of pixels with blown highlights or crushed shadows.
    # Checked on luminance so saturated colours (e.g. a red flower) don't false-fire.
    clip_ratio = float(((luma <= 2) | (luma >= 253)).mean())

    # RMS contrast — std dev of luminance
    rms_contrast = float(luma.std())

    return sharpness, clip_ratio, rms_contrast


def run_script(api):
    """Reorder displayed images from worst to best estimated visual quality."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        api.show_message("numpy and Pillow are required for visual quality sort.")
        return

    all_images = api.get_all_images()
    if not all_images:
        return

    metadata = api.get_metadata_batch(all_images)
    api.show_message(f"Analysing {len(all_images)} thumbnails…", timeout=30000)

    paths_with_metrics = []
    raw_metrics = []
    no_thumb = []

    for path in all_images:
        thumb = (metadata.get(path) or {}).get("thumbnail_path")
        if not thumb:
            no_thumb.append(path)
            continue
        try:
            arr = np.array(
                Image.open(thumb).convert("RGB"),  # disk-io: local thumbnail cache
                dtype=np.float32,
            )
        except Exception:
            no_thumb.append(path)
            continue
        paths_with_metrics.append(path)
        raw_metrics.append(_compute_metrics(arr, np))

    if not paths_with_metrics:
        api.show_message("No cached thumbnails found — generate thumbnails first.")
        return

    m = np.array(raw_metrics, dtype=np.float64)  # (N, 3): sharpness, clip_ratio, rms_contrast

    def _norm(col):
        lo, hi = col.min(), col.max()
        # N=1 or all-identical: every score is 0, order is unchanged — correct
        return (col - lo) / (hi - lo) if hi > lo else np.zeros_like(col)

    quality = (
        _W_SHARPNESS * _norm(m[:, 0])
        - _W_CLIPPING * _norm(m[:, 1])
        + _W_CONTRAST * _norm(m[:, 2])
    )

    order = np.argsort(quality)  # ascending: worst first
    sorted_paths = [paths_with_metrics[i] for i in order] + no_thumb

    if sorted_paths != all_images:
        api.set_image_order(sorted_paths)
