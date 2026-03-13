"""Face detection (SCRFD) and recognition (ArcFace) via ONNX Runtime.

Qt-free, daemon-safe. Follows core/clip_inference.py / core/orientation_model.py patterns.
Graceful no-op if onnxruntime or numpy is not installed.
"""
import logging
import os
from typing import List, Optional

from core import onnx_runtime
from core.model_manager import get_model_path, ensure_model

logger = logging.getLogger(__name__)

_DET_MODEL_KEY = "face-detection-buffalo_l"
_REC_MODEL_KEY = "face-recognition-buffalo_l"

# SCRFD input size
_DET_SIZE = 640

# ArcFace aligned crop size
_ALIGN_SIZE = 112

# InsightFace standard 5-point template for 112x112 alignment
_ARCFACE_DST = [
    (38.2946, 51.6963),
    (73.5318, 51.5014),
    (56.0252, 71.7366),
    (41.5493, 92.3655),
    (70.7299, 92.2041),
]

# SCRFD anchor strides and feature map config
_STRIDES = [8, 16, 32]
_NMS_THRESH = 0.4

# Minimum normalized bbox dimension and area — faces smaller than this are too
# small for reliable recognition and are usually background noise or texture
# false positives.  The area check catches elongated skin-patch detections that
# pass the per-dimension check.
_MIN_BBOX_DIM = 0.025
_MIN_BBOX_AREA = 0.003

# Minimum ArcFace embedding norm before L2 normalization.  Real faces produce
# norms ~15-30; non-face crops (skin, foliage, textures) produce lower norms.
_MIN_EMBEDDING_NORM = 12.0


def _get_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def _get_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _generate_anchors(height, width, stride):
    """Generate anchor centers for a single stride level."""
    np = _get_numpy()
    anchor_centers = np.stack(
        np.mgrid[:height, :width][::-1], axis=-1
    ).astype(np.float32).reshape(-1, 2)
    anchor_centers = (anchor_centers * stride).reshape(-1, 2)
    # SCRFD uses 2 anchors per location
    anchor_centers = np.stack([anchor_centers, anchor_centers], axis=1).reshape(-1, 2)
    return anchor_centers


def _nms(boxes, scores, thresh):
    """Non-maximum suppression."""
    np = _get_numpy()
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(iou <= thresh)[0]
        order = order[inds + 1]
    return keep


def _distance2bbox(anchor_centers, distance):
    np = _get_numpy()
    x1 = anchor_centers[:, 0] - distance[:, 0]
    y1 = anchor_centers[:, 1] - distance[:, 1]
    x2 = anchor_centers[:, 0] + distance[:, 2]
    y2 = anchor_centers[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(anchor_centers, distance):
    np = _get_numpy()
    kps = distance.copy()
    for i in range(0, kps.shape[1], 2):
        kps[:, i] = anchor_centers[:, 0] + kps[:, i]
        kps[:, i + 1] = anchor_centers[:, 1] + kps[:, i + 1]
    return kps


def _load_image(image_path, view_image_path, Image, max_size=None):
    """Load image from view cache or source, optionally capping longest edge."""
    source = view_image_path if (view_image_path and os.path.isfile(view_image_path)) else image_path  # disk-io: prefer local cache
    try:
        img = Image.open(source)  # disk-io: load image for face processing
        img = img.convert("RGB")
    except Exception:  # why: PIL raises various decoder exceptions
        logger.debug("Failed to open image: %s", source, exc_info=True)
        return None
    if max_size:
        w, h = img.size
        longest = max(w, h)
        if longest > max_size:
            scale = max_size / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    return img


# Max image dimension for detection — SCRFD downscales to 640×640 anyway,
# so loading anything larger than this wastes memory.
_DET_MAX_SIZE = 1280


def _valid_face_geometry(keypoints):
    """Reject detections where the 5 keypoints don't form a plausible face.

    Keypoint order: left_eye, right_eye, nose, left_mouth, right_mouth.
    Checks: eyes roughly horizontal, nose below eyes, mouth below nose,
    eyes flanking nose horizontally.
    """
    le, re, nose, lm, rm = keypoints

    # Eyes should be roughly at the same height (within 40% of eye distance)
    eye_dx = abs(re[0] - le[0])
    if eye_dx < 1e-6:
        return False
    eye_dy = abs(re[1] - le[1])
    if eye_dy / eye_dx > 0.6:
        return False

    # Nose should be below (or level with) the eye midpoint
    eye_mid_y = (le[1] + re[1]) / 2
    if nose[1] < eye_mid_y - eye_dx * 0.3:
        return False

    # Mouth midpoint should be below (or level with) the nose
    mouth_mid_y = (lm[1] + rm[1]) / 2
    if mouth_mid_y < nose[1] - eye_dx * 0.3:
        return False

    # Nose x should be between the eyes (with margin)
    eye_left_x = min(le[0], re[0])
    eye_right_x = max(le[0], re[0])
    margin = eye_dx * 0.5
    if nose[0] < eye_left_x - margin or nose[0] > eye_right_x + margin:
        return False

    return True


def detect_faces(image_path: str, view_image_path: str = None,
                 config_manager=None, confidence_threshold: float = 0.5) -> Optional[List[dict]]:
    """Detect faces in an image. Returns list of {bbox, keypoints, confidence} or None on error.

    bbox is (x, y, w, h) normalized 0-1. keypoints is [(x, y), ...] normalized 0-1.
    Uses view_image_path (full-res local cache) when available to avoid NAS reads.
    """
    np = _get_numpy()
    Image = _get_pil()
    if np is None or Image is None or not onnx_runtime.is_available():
        return None

    model_path = get_model_path(_DET_MODEL_KEY, config_manager)
    if not model_path or not os.path.isfile(model_path):  # disk-io: local model cache check
        model_path = ensure_model(_DET_MODEL_KEY, config_manager)
    if not model_path:
        return None

    session = onnx_runtime.get_session(model_path)
    if session is None:
        return None

    # Cap at _DET_MAX_SIZE for detection — model input is 640×640 anyway
    img = _load_image(image_path, view_image_path, Image, max_size=_DET_MAX_SIZE)
    if img is None:
        return None

    orig_w, orig_h = img.size

    # Letterbox resize to _DET_SIZE x _DET_SIZE
    scale = min(_DET_SIZE / orig_w, _DET_SIZE / orig_h)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)
    img_resized = img.resize((new_w, new_h), Image.BILINEAR)

    # Pad to _DET_SIZE x _DET_SIZE and normalize: (pixel - 127.5) / 128.0
    # This matches InsightFace's cv2.dnn.blobFromImage preprocessing.
    arr = np.full((_DET_SIZE, _DET_SIZE, 3), -127.5 / 128.0, dtype=np.float32)
    pixel_data = np.array(img_resized, dtype=np.float32)
    arr[:new_h, :new_w, :] = (pixel_data - 127.5) / 128.0

    # HWC -> CHW, add batch
    input_tensor = arr.transpose(2, 0, 1)[np.newaxis, ...]

    try:
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: input_tensor})
    except Exception:  # why: onnxruntime C++ exceptions
        logger.error("Face detection inference failed: %s", image_path, exc_info=True)
        return None

    # SCRFD outputs: for each stride, (scores, bboxes, keypoints)
    # 3 strides × 3 outputs = 9 total outputs
    all_scores = []
    all_bboxes = []
    all_kps = []

    for idx, stride in enumerate(_STRIDES):
        fh = _DET_SIZE // stride
        fw = _DET_SIZE // stride
        anchors = _generate_anchors(fh, fw, stride)

        scores = outputs[idx][:, :, 1] if outputs[idx].shape[-1] == 2 else outputs[idx]
        scores = scores.flatten()

        bbox_pred = outputs[idx + len(_STRIDES)] * stride
        bbox_pred = bbox_pred.reshape(-1, 4)
        bboxes = _distance2bbox(anchors, bbox_pred)

        kps_pred = outputs[idx + len(_STRIDES) * 2] * stride
        kps_pred = kps_pred.reshape(-1, 10)
        kps = _distance2kps(anchors, kps_pred)

        all_scores.append(scores)
        all_bboxes.append(bboxes)
        all_kps.append(kps)

    scores = np.concatenate(all_scores)
    bboxes = np.concatenate(all_bboxes)
    kps = np.concatenate(all_kps)

    # Filter by confidence
    mask = scores >= confidence_threshold
    scores = scores[mask]
    bboxes = bboxes[mask]
    kps = kps[mask]

    if len(scores) == 0:
        return []

    # NMS
    keep = _nms(bboxes, scores, _NMS_THRESH)
    bboxes = bboxes[keep]
    scores = scores[keep]
    kps = kps[keep]

    results = []
    for i in range(len(scores)):
        # Convert from letterbox pixel coords to normalized 0-1
        x1 = float(bboxes[i][0] / scale / orig_w)
        y1 = float(bboxes[i][1] / scale / orig_h)
        x2 = float(bboxes[i][2] / scale / orig_w)
        y2 = float(bboxes[i][3] / scale / orig_h)

        # Clamp to [0, 1]
        x1 = max(0.0, min(1.0, x1))
        y1 = max(0.0, min(1.0, y1))
        x2 = max(0.0, min(1.0, x2))
        y2 = max(0.0, min(1.0, y2))

        bw = x2 - x1
        bh = y2 - y1
        if bw < _MIN_BBOX_DIM or bh < _MIN_BBOX_DIM or bw * bh < _MIN_BBOX_AREA:
            continue

        keypoints = []
        for j in range(5):
            kx = float(kps[i][j * 2] / scale / orig_w)
            ky = float(kps[i][j * 2 + 1] / scale / orig_h)
            keypoints.append((max(0.0, min(1.0, kx)), max(0.0, min(1.0, ky))))

        if not _valid_face_geometry(keypoints):
            continue

        results.append({
            'bbox': (x1, y1, bw, bh),
            'keypoints': keypoints,
            'confidence': float(scores[i]),
        })

    return results


def _estimate_affine(src_pts, dst_pts):
    """Estimate 2x3 affine transform from src to dst (both Nx2). Numpy-only, no skimage."""
    np = _get_numpy()
    n = src_pts.shape[0]
    # Build system: [x y 1 0 0 0; 0 0 0 x y 1] * [a b c d e f]^T = [dx dy]
    A = np.zeros((2 * n, 6), dtype=np.float64)
    b = np.zeros(2 * n, dtype=np.float64)
    for i in range(n):
        A[2 * i] = [src_pts[i, 0], src_pts[i, 1], 1, 0, 0, 0]
        A[2 * i + 1] = [0, 0, 0, src_pts[i, 0], src_pts[i, 1], 1]
        b[2 * i] = dst_pts[i, 0]
        b[2 * i + 1] = dst_pts[i, 1]
    result, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    M = np.array([[result[0], result[1], result[2]],
                   [result[3], result[4], result[5]]], dtype=np.float64)
    return M


def _align_face(image, keypoints):
    """Align face using 5-point affine transformation to 112x112 canonical template.

    image: PIL Image. keypoints: [(x, y), ...] in pixel coordinates.
    Returns (112, 112, 3) uint8 ndarray.
    """
    np = _get_numpy()
    src = np.array(keypoints, dtype=np.float64)
    dst = np.array(_ARCFACE_DST, dtype=np.float64)

    M = _estimate_affine(src, dst)

    # Apply affine transform manually (no cv2/skimage dependency)
    img_arr = np.array(image, dtype=np.uint8)
    h_out, w_out = _ALIGN_SIZE, _ALIGN_SIZE
    result = np.zeros((h_out, w_out, 3), dtype=np.uint8)

    # Inverse mapping for better quality
    M_inv = np.zeros((2, 3), dtype=np.float64)
    # Invert the 2x2 part
    a, b, c = M[0]
    d, e, f = M[1]
    det = a * e - b * d
    if abs(det) < 1e-10:
        return result
    M_inv[0, 0] = e / det
    M_inv[0, 1] = -b / det
    M_inv[0, 2] = (b * f - c * e) / det
    M_inv[1, 0] = -d / det
    M_inv[1, 1] = a / det
    M_inv[1, 2] = (c * d - a * f) / det

    h_in, w_in = img_arr.shape[:2]
    # Generate output coordinate grid
    yy, xx = np.mgrid[:h_out, :w_out]
    xx = xx.astype(np.float64)
    yy = yy.astype(np.float64)
    src_x = M_inv[0, 0] * xx + M_inv[0, 1] * yy + M_inv[0, 2]
    src_y = M_inv[1, 0] * xx + M_inv[1, 1] * yy + M_inv[1, 2]

    # Nearest-neighbor sampling
    src_x_int = np.clip(np.round(src_x).astype(np.int32), 0, w_in - 1)
    src_y_int = np.clip(np.round(src_y).astype(np.int32), 0, h_in - 1)
    valid = (src_x >= -0.5) & (src_x < w_in - 0.5) & (src_y >= -0.5) & (src_y < h_in - 0.5)

    result[valid] = img_arr[src_y_int[valid], src_x_int[valid]]
    return result


def extract_embedding(aligned_crop, config_manager=None):
    """Extract 512-dim L2-normalized embedding from a 112x112 aligned face crop.

    aligned_crop: (112, 112, 3) uint8 ndarray.
    Returns (512,) float32 ndarray, or None.  Returns None for non-face inputs
    (raw embedding norm below threshold).
    """
    np = _get_numpy()
    if np is None or not onnx_runtime.is_available():
        return None

    model_path = get_model_path(_REC_MODEL_KEY, config_manager)
    if not model_path or not os.path.isfile(model_path):  # disk-io: local model cache check
        model_path = ensure_model(_REC_MODEL_KEY, config_manager)
    if not model_path:
        return None

    session = onnx_runtime.get_session(model_path)
    if session is None:
        return None

    # Normalize: (pixel / 127.5) - 1.0, transpose to CHW
    arr = aligned_crop.astype(np.float32)
    arr = (arr / 127.5) - 1.0
    arr = arr.transpose(2, 0, 1)
    arr = np.expand_dims(arr, 0)

    try:
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: arr})
        embedding = outputs[0].flatten().astype(np.float32)

        # Raw norm before L2 normalization correlates with face quality —
        # real faces typically produce norms > 15, non-faces < 15.
        norm = float(np.linalg.norm(embedding))
        if norm < _MIN_EMBEDDING_NORM:
            logger.debug("Rejected face: low embedding norm %.1f", norm)
            return None
        if norm > 0:
            embedding = embedding / norm
        return embedding
    except Exception:  # why: onnxruntime C++ exceptions
        logger.error("Face embedding extraction failed", exc_info=True)
        return None


# CLIP zero-shot face verification — rejects non-human detections (animals,
# textures, skin patches) by classifying the raw crop against multiple prompts.
_clip_class_embs = None
_CLIP_CLASS_PROMPTS = [
    "a photo of a human face",
    "a photo of an animal",
    "a photo of skin or body part",
    "a photo of nature, foliage, or landscape",
    "a photo of a rock or stone texture",
]
# Minimum softmax probability for "human face" class.  Calibrated on wildlife/
# outdoor photography: real faces score 40-93%, false positives score <30%.
_CLIP_HUMAN_MIN_PROB = 0.3


def _clip_is_human_face(crop_pil, config_manager=None):
    """Single-crop convenience wrapper around _clip_filter_batch."""
    results = _clip_filter_batch([crop_pil], config_manager)
    return results[0] if results else True


def _clip_filter_batch(crop_pils, config_manager=None):
    """Zero-shot CLIP classification on a batch of crops.

    Returns list of bools (True = human face, False = rejected).
    Single CLIP forward pass for the entire batch.
    Falls back to all-True if CLIP is unavailable.
    """
    global _clip_class_embs
    np = _get_numpy()
    if np is None or not crop_pils:
        return [True] * len(crop_pils)

    from core import clip_inference
    if not onnx_runtime.is_available():
        return [True] * len(crop_pils)

    # Lazy-compute class text embeddings (cached across calls)
    if _clip_class_embs is None:
        embs = []
        for prompt in _CLIP_CLASS_PROMPTS:
            emb = clip_inference.encode_text(prompt, config_manager)
            if emb is None:
                return [True] * len(crop_pils)
            embs.append(emb)
        _clip_class_embs = np.stack(embs)  # (N_classes, 512)

    # Preprocess all crops into a single batch tensor
    from core.clip_inference import _CLIP_IMAGE_SIZE, _CLIP_MEAN, _CLIP_STD
    Image = _get_pil()
    if Image is None:
        return [True] * len(crop_pils)

    batch = np.zeros((len(crop_pils), 3, _CLIP_IMAGE_SIZE, _CLIP_IMAGE_SIZE), dtype=np.float32)
    for i, crop in enumerate(crop_pils):
        img = crop.resize((_CLIP_IMAGE_SIZE, _CLIP_IMAGE_SIZE), Image.BICUBIC)
        arr = np.array(img, dtype=np.float32) / 255.0
        for c in range(3):
            arr[:, :, c] = (arr[:, :, c] - _CLIP_MEAN[c]) / _CLIP_STD[c]
        batch[i] = arr.transpose(2, 0, 1)

    # Single CLIP forward pass for entire batch
    model_path = get_model_path("clip-vit-b-32-visual", config_manager)
    if not model_path:
        return [True] * len(crop_pils)
    session = onnx_runtime.get_session(model_path)
    if session is None:
        return [True] * len(crop_pils)

    try:
        input_name = session.get_inputs()[0].name
        results = []
        for i in range(len(crop_pils)):
            # Run one crop at a time (CLIP ViT has fixed batch=1 input)
            single = batch[i:i+1]  # (1, 3, 224, 224)
            outputs = session.run(None, {input_name: single})
            img_emb = outputs[0].flatten().astype(np.float32)
            norm = np.linalg.norm(img_emb)
            if norm > 0:
                img_emb = img_emb / norm

            # Zero-shot classification: softmax over class similarities
            sims = _clip_class_embs @ img_emb  # (N_classes,)
            logits = sims * 100.0  # CLIP logit scale
            exp_logits = np.exp(logits - logits.max())
            probs = exp_logits / exp_logits.sum()
            human_prob = float(probs[0])

            if human_prob < _CLIP_HUMAN_MIN_PROB:
                top_idx = int(np.argmax(probs))
                top_label = _CLIP_CLASS_PROMPTS[top_idx].split("of ")[-1]
                logger.debug("CLIP rejected face: human=%.0f%% top=%s(%.0f%%)",
                             human_prob * 100, top_label, probs[top_idx] * 100)
                results.append(False)
            else:
                results.append(True)
        return results
    except Exception:
        logger.debug("CLIP batch verification failed", exc_info=True)
        return [True] * len(crop_pils)


def detect_and_embed(image_path: str, view_image_path: str = None,
                     config_manager=None, confidence_threshold: float = 0.7) -> Optional[List[dict]]:
    """Detect faces and extract embeddings. Main entry point.

    Returns [{bbox, keypoints, confidence, embedding}, ...] or None on error.
    Uses view_image_path (full-res local cache) when available.
    """
    np = _get_numpy()
    Image = _get_pil()
    if np is None or Image is None:
        return None

    faces = detect_faces(image_path, view_image_path, config_manager, confidence_threshold)
    if faces is None:
        return None
    if not faces:
        return []

    # Load full-resolution image for alignment — higher res = sharper 112×112 crops
    img = _load_image(image_path, view_image_path, Image)
    if img is None:
        return None

    w, h = img.size
    results = []
    for face in faces:
        kps_pixel = [(kp[0] * w, kp[1] * h) for kp in face['keypoints']]
        aligned = _align_face(img, kps_pixel)
        embedding = extract_embedding(aligned, config_manager)
        if embedding is None:
            continue

        results.append({
            'bbox': face['bbox'],
            'keypoints': face['keypoints'],
            'confidence': face['confidence'],
            'embedding': embedding,
        })

    return results


def embedding_to_bytes(embedding) -> bytes:
    """Raw float32 bytes for SQLite BLOB storage."""
    return embedding.tobytes()


def bytes_to_embedding(data: bytes):
    """Inverse of embedding_to_bytes."""
    np = _get_numpy()
    if np is None:
        return None
    return np.frombuffer(data, dtype=np.float32).copy()
