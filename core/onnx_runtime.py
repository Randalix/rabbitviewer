"""Thin wrapper around onnxruntime for ONNX session management.

Qt-free, daemon-safe. Graceful no-op if onnxruntime is not installed.
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_sessions = {}
_lock = threading.Lock()


def is_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def get_models_dir(config_manager=None) -> str:
    if config_manager:
        d = config_manager.get("ai.models_dir", "~/.rabbitviewer/models")
    else:
        d = "~/.rabbitviewer/models"
    d = os.path.expanduser(d)
    os.makedirs(d, exist_ok=True)
    return d


def get_session(model_path: str) -> Optional[object]:
    if not is_available():
        return None
    if not os.path.isfile(model_path):  # disk-io: local model cache check
        logger.warning("ONNX model not found: %s", model_path)
        return None

    with _lock:
        if model_path in _sessions:
            logger.debug("ONNX session cache hit: %s", model_path)
            return _sessions[model_path]

        import onnxruntime as ort

        providers = []
        available = ort.get_available_providers()
        if "CoreMLExecutionProvider" in available:
            providers.append("CoreMLExecutionProvider")
        providers.append("CPUExecutionProvider")

        try:
            logger.info("Creating ONNX session: %s (providers=%s)", model_path, providers)
            t0 = time.perf_counter()
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 2
            session = ort.InferenceSession(model_path, opts, providers=providers)
            logger.info("ONNX session created in %.3fs: %s", time.perf_counter() - t0, model_path)
        except Exception:  # why: onnxruntime raises undocumented C++ exceptions on CoreML init failure, model format errors, provider mismatches
            logger.error("Failed to load ONNX model: %s", model_path, exc_info=True)
            return None

        _sessions[model_path] = session
        return session


def prewarm_session(model_path: str) -> None:
    """Load an ONNX session in a background thread so it's cached for first use."""
    if not is_available():
        return
    if not os.path.isfile(model_path):  # disk-io: local model cache check
        return

    def _warm():
        try:
            get_session(model_path)
        except Exception:  # why: best-effort warmup; missing model or runtime error must not surface
            logger.debug("Prewarm failed for %s", model_path, exc_info=True)

    threading.Thread(target=_warm, daemon=True, name="onnx-prewarm").start()


def release_all():
    with _lock:
        _sessions.clear()
