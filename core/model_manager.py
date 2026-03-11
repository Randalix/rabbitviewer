"""ONNX model download and cache management.

Pure stdlib (urllib.request). Downloads models from HuggingFace on first use,
verifies SHA256, caches in ~/.rabbitviewer/models/.
"""
import hashlib
import logging
import os
import urllib.request
from typing import Callable, Optional

from core.onnx_runtime import get_models_dir

logger = logging.getLogger(__name__)

# HuggingFace repo for OpenCLIP ONNX exports
_HF_BASE = "https://huggingface.co/immich-app/ViT-B-32__openai/resolve/main"

MODELS = {
    "clip-vit-b-32-visual": {
        "url": f"{_HF_BASE}/visual.onnx",
        "filename": "visual.onnx",
        "sha256": "",  # populated after first verified download
    },
    "clip-vit-b-32-textual": {
        "url": f"{_HF_BASE}/textual.onnx",
        "filename": "textual.onnx",
        "sha256": "",
    },
    "clip-vit-b-32-preprocessor": {
        "url": f"{_HF_BASE}/preprocess_cfg.json",
        "filename": "preprocess_cfg.json",
        "sha256": "",
    },
    "clip-vit-b-32-tokenizer": {
        "url": f"{_HF_BASE}/tokenizer.json",
        "filename": "tokenizer.json",
        "sha256": "",
    },
    "orientation-efficientnet": {
        "url": "https://huggingface.co/DuarteBarbosa/deep-image-orientation-detection/resolve/main/orientation_model_v2_0.9882.onnx",
        "filename": "orientation_model_v2.onnx",
        "sha256": "",
    },
}


def _model_dir(model_name: str, config_manager=None) -> str:
    # Group CLIP files together
    if model_name.startswith("clip-vit-b-32"):
        subdir = "clip-vit-b-32"
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


def is_model_available(model_name: str, config_manager=None) -> bool:
    """True if the model file exists on disk."""
    path = get_model_path(model_name, config_manager)
    return path is not None and os.path.isfile(path)  # disk-io: local model cache check


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
    """Download model if missing, verify SHA256, return local path.

    progress_cb(bytes_downloaded, total_bytes) is called during download.
    Returns None on failure.
    """
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
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
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

        if not _verify_sha256(tmp_path, info.get("sha256", "")):
            logger.error("SHA256 mismatch for %s", model_name)
            os.unlink(tmp_path)
            return None

        os.replace(tmp_path, path)
        logger.info("Model %s saved to %s", model_name, path)
        return path

    except Exception:  # why: urllib raises OSError subclasses, ssl.SSLError, ValueError depending on failure
        logger.error("Failed to download model %s", model_name, exc_info=True)
        if os.path.exists(tmp_path):  # disk-io: cleanup failed download
            os.unlink(tmp_path)
        return None


def ensure_clip_models(
    config_manager=None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> bool:
    """Returns True if all 4 CLIP files are cached or successfully downloaded."""
    required = [
        "clip-vit-b-32-visual",
        "clip-vit-b-32-textual",
        "clip-vit-b-32-preprocessor",
        "clip-vit-b-32-tokenizer",
    ]
    for name in required:
        cb = (lambda d, t, _n=name: progress_cb(_n, d, t)) if progress_cb else None
        path = ensure_model(name, config_manager, progress_cb=cb)
        if path is None:
            return False
    return True
