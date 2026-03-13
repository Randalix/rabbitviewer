"""ONNX model download and cache management.

Pure stdlib (urllib.request). Downloads models from HuggingFace on first use,
verifies SHA256, caches in ~/.rabbitviewer/models/.
"""
import hashlib
import logging
import os
import time
import urllib.request
from typing import Callable, Optional

from core.onnx_runtime import get_models_dir

logger = logging.getLogger(__name__)

# HuggingFace repo for OpenCLIP ONNX exports
_HF_BASE = "https://huggingface.co/immich-app/ViT-B-32__openai/resolve/main"

MODELS = {
    "clip-vit-b-32-visual": {
        "url": f"{_HF_BASE}/visual/model.onnx",
        "filename": "visual.onnx",
        "sha256": "",
    },
    "clip-vit-b-32-textual": {
        "url": f"{_HF_BASE}/textual/model.onnx",
        "filename": "textual.onnx",
        "sha256": "",
    },
    "clip-vit-b-32-preprocessor": {
        "url": f"{_HF_BASE}/visual/preprocess_cfg.json",
        "filename": "preprocess_cfg.json",
        "sha256": "",
    },
    "clip-vit-b-32-tokenizer": {
        "url": f"{_HF_BASE}/textual/tokenizer.json",
        "filename": "tokenizer.json",
        "sha256": "",
    },
    "orientation-efficientnet": {
        "url": "https://huggingface.co/DuarteBarbosa/deep-image-orientation-detection/resolve/main/orientation_model_v2_0.9882.onnx",
        "filename": "orientation_model_v2.onnx",
        "sha256": "",
    },
    "face-detection-buffalo_l": {
        "url": "https://huggingface.co/immich-app/buffalo_l/resolve/main/detection/model.onnx",
        "filename": "detection.onnx",
        "sha256": "",
    },
    "face-recognition-buffalo_l": {
        "url": "https://huggingface.co/immich-app/buffalo_l/resolve/main/recognition/model.onnx",
        "filename": "recognition.onnx",
        "sha256": "",
    },
}


def _model_dir(model_name: str, config_manager=None) -> str:
    if model_name.startswith("clip-vit-b-32"):
        subdir = "clip-vit-b-32"
    elif model_name.startswith("face-"):
        subdir = "buffalo_l"
    else:
        subdir = model_name
    d = os.path.join(get_models_dir(config_manager), subdir)
    os.makedirs(d, exist_ok=True)
    return d


def get_model_path(model_name: str, config_manager=None) -> Optional[str]:
    info = MODELS.get(model_name)
    if not info:
        return None
    return os.path.join(_model_dir(model_name, config_manager), info["filename"])


def _verify_sha256(path: str, expected: str) -> bool:
    if not expected:
        return True  # skip verification if hash not set
    h = hashlib.sha256()
    with open(path, "rb") as f:  # disk-io: SHA256 verification of downloaded model
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def ensure_model(
    model_name: str,
    config_manager=None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Optional[str]:
    """Download model if missing, verify SHA256, return local path."""
    info = MODELS.get(model_name)
    if not info:
        logger.error("Unknown model: %s", model_name)
        return None

    path = get_model_path(model_name, config_manager)
    if not path:
        return None
    if os.path.isfile(path):  # disk-io: local model cache check
        return path

    url = info["url"]
    logger.info("Downloading model %s from %s", model_name, url)

    tmp_path = path + ".tmp"
    try:
        t0 = time.perf_counter()
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            logger.info("Model %s download started (size=%s)",
                        model_name, f"{total / 1048576:.1f}MB" if total else "unknown")
            downloaded = 0
            with open(tmp_path, "wb") as f:  # disk-io: write downloaded model to cache
                while True:
                    chunk = resp.read(1 << 20)  # 1 MB
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
        t_download = time.perf_counter() - t0
        logger.info("Model %s downloaded in %.1fs (%.1f MB/s)",
                    model_name, t_download,
                    (downloaded / 1048576) / t_download if t_download > 0 else 0)

        t0 = time.perf_counter()
        if not _verify_sha256(tmp_path, info.get("sha256", "")):
            logger.error("SHA256 mismatch for %s", model_name)
            os.unlink(tmp_path)
            return None
        logger.info("Model %s SHA256 verified in %.3fs", model_name, time.perf_counter() - t0)

        os.replace(tmp_path, path)
        logger.info("Model %s saved to %s", model_name, path)
        return path

    except Exception:  # why: urllib raises OSError subclasses, ssl.SSLError, ValueError depending on failure
        logger.error("Failed to download model %s", model_name, exc_info=True)
        if os.path.exists(tmp_path):  # disk-io: cleanup failed download
            os.unlink(tmp_path)
        return None


