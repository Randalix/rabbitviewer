"""Orientation detection model via ONNX Runtime.

Qt-free. Classifies images into 4 rotation classes (0/90/180/270 degrees)
and maps to EXIF Orientation values.  Uses EfficientNetV2-S (384x384 input).
"""
import logging
import os
import time
from typing import Optional, Tuple

from core import onnx_runtime
from core.model_manager import get_model_path, ensure_model

logger = logging.getLogger(__name__)

_MODEL_KEY = "orientation-efficientnet"

# EfficientNetV2-S preprocessing: resize to 416, center-crop to 384
_RESIZE = 416
_CROP = 384

# Model output class index → EXIF Orientation tag value
# Class 0 = upright, Class 1 = 90° CCW applied → correct with 90° CW (EXIF 6)
# Class 2 = 180°, Class 3 = 270° CCW applied → correct with 270° CW (EXIF 8)
_CLASS_TO_EXIF = {0: 1, 1: 6, 2: 3, 3: 8}

# EXIF Orientation tag value → rotation degrees (for display/logging)
EXIF_TO_DEGREES = {1: 0, 6: 90, 3: 180, 8: 270}


def _get_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def predict_orientation(
    image_path: str,
    thumbnail_path: Optional[str] = None,
    config_manager=None,
) -> Optional[Tuple[int, float]]:
    """Returns (exif_orientation, confidence) or None if model unavailable.

    Uses cached thumbnail to avoid NAS reads.
    """
    np = _get_numpy()
    if np is None or not onnx_runtime.is_available():
        return None

    t0 = time.perf_counter()
    model_path = get_model_path(_MODEL_KEY, config_manager)
    if not model_path or not os.path.isfile(model_path):  # disk-io: local model cache check
        model_path = ensure_model(_MODEL_KEY, config_manager)
    if not model_path:
        return None
    t_model_resolve = time.perf_counter() - t0

    t0 = time.perf_counter()
    session = onnx_runtime.get_session(model_path)
    if session is None:
        return None
    t_session = time.perf_counter() - t0

    source = thumbnail_path if (thumbnail_path and os.path.isfile(thumbnail_path)) else image_path  # disk-io: prefer local cache over NAS

    try:
        from PIL import Image
        t0 = time.perf_counter()
        img = Image.open(source).convert("RGB")  # disk-io: load cached thumbnail or source for orientation detection
        t_load = time.perf_counter() - t0
    except Exception:  # why: PIL raises UnidentifiedImageError, OSError, and arbitrary decoder exceptions
        logger.debug("Failed to open image for orientation: %s", source)
        return None

    try:
        # Resize shortest side to _RESIZE, then center crop to _CROP
        t0 = time.perf_counter()
        w, h = img.size
        if w < h:
            new_w = _RESIZE
            new_h = int(h * _RESIZE / w)
        else:
            new_h = _RESIZE
            new_w = int(w * _RESIZE / h)
        img = img.resize((new_w, new_h), Image.BICUBIC)

        left = (new_w - _CROP) // 2
        top = (new_h - _CROP) // 2
        img = img.crop((left, top, left + _CROP, top + _CROP))

        arr = np.array(img, dtype=np.float32) / 255.0
        # ImageNet normalization
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        arr = (arr - mean) / std
        arr = arr.transpose(2, 0, 1)  # HWC → CHW
        arr = np.expand_dims(arr, 0)   # add batch dim
        t_preprocess = time.perf_counter() - t0

        t0 = time.perf_counter()
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: arr})
        t_inference = time.perf_counter() - t0
        logits = outputs[0].flatten()

        # Softmax for confidence
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / exp_logits.sum()

        class_idx = int(np.argmax(probs))
        confidence = float(probs[class_idx])
        exif_orient = _CLASS_TO_EXIF.get(class_idx, 1)

        logger.info("predict_orientation: resolve=%.3fs session=%.3fs load=%.3fs "
                    "preprocess=%.3fs inference=%.3fs (source=%s)",
                    t_model_resolve, t_session, t_load,
                    t_preprocess, t_inference,
                    "thumbnail" if source == thumbnail_path else "original")

        return (exif_orient, confidence)
    except Exception:  # why: onnxruntime raises undocumented C++ exceptions on inference failure
        logger.error("Orientation prediction failed: %s", source, exc_info=True)
        return None
