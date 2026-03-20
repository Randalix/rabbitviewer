"""Thin wrapper around onnxruntime for ONNX session management.

Qt-free, daemon-safe. Graceful no-op if onnxruntime is not installed.
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_sessions: dict = {}
_model_locks: dict = {}
# Guards _sessions and _model_locks dicts only — not held during model creation.
_registry_lock = threading.Lock()


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

    # Fast path: session already cached.
    with _registry_lock:
        if model_path in _sessions:
            logger.debug("ONNX session cache hit: %s", model_path)
            return _sessions[model_path]
        # Acquire (or create) the per-model lock while holding the registry
        # lock, so two threads racing on the same model both get the same lock
        # object and one waits while the other loads.
        if model_path not in _model_locks:
            _model_locks[model_path] = threading.Lock()
        model_lock = _model_locks[model_path]

    # Per-model lock: only one thread loads a given model; other models load concurrently.
    with model_lock:
        # Re-check: another thread may have loaded while we waited.
        with _registry_lock:
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
            opts.intra_op_num_threads = 1

            # Enable graph optimization caching. The first run will be slow as it
            # saves the optimized graph; subsequent runs will be much faster.
            optimized_model_path = model_path + ".ort"
            opts.optimized_model_filepath = optimized_model_path

            session = ort.InferenceSession(model_path, opts, providers=providers)
            logger.info("ONNX session created in %.3fs: %s", time.perf_counter() - t0, model_path)
        except Exception:  # why: onnxruntime raises undocumented C++ exceptions on CoreML init failure, model format errors, provider mismatches
            logger.error("Failed to load ONNX model: %s", model_path, exc_info=True)
            return None

        with _registry_lock:
            _sessions[model_path] = session
        return session


def _set_background_priority() -> None:
    """Drop the calling thread to background OS priority.

    On macOS: QOS_CLASS_BACKGROUND — scheduler gives spare cycles only.
    On Linux: nice(10) — lower than interactive but not completely starved.
    Silently ignored on other platforms or if the syscall fails.
    """
    try:
        if os.name == "posix":
            import sys
            if sys.platform == "darwin":
                import ctypes, ctypes.util
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                # pthread_set_qos_class_self_np(QOS_CLASS_BACKGROUND=0x09, 0)
                libc.pthread_set_qos_class_self_np(0x09, 0)
            else:
                os.nice(10)
    except Exception:
        pass  # best-effort; never block on priority failure


def get_session_bg(model_path: str) -> Optional[object]:
    """Like get_session(), but guarantees first load runs at background OS priority.

    If the session is already cached, returns it directly (no thread overhead).
    If not, spawns a short-lived background-priority thread to load it and blocks
    until done. This prevents CoreML from calling dispatch_sync(main_queue) and
    freezing the Qt event loop — CoreML at QOS_CLASS_BACKGROUND avoids GPU/ANE
    code paths that require main-thread Metal initialization.

    Call sites are worker threads that must not be permanently demoted to background
    priority (they also run thumbnail work), so the demotion is isolated to the
    loader thread.
    """
    # Fast path: already cached — return without spawning a thread.
    with _registry_lock:
        if model_path in _sessions:
            return _sessions[model_path]

    # Slow path: first load — isolate in a background-priority thread.
    result: list[Optional[object]] = [None]
    done = threading.Event()

    def _load():
        _set_background_priority()
        result[0] = get_session(model_path)
        done.set()

    threading.Thread(target=_load, daemon=True, name="onnx-load-bg").start()
    done.wait()
    return result[0]


def prewarm_session(model_path: str) -> None:
    """Load an ONNX session in a background thread so it's cached for first use.

    The thread runs at background OS priority so CoreML compilation never
    competes with the UI thread or active render workers.  All work (including
    onnxruntime import and CoreML dylib init) is deferred to the background
    thread — calling this from any thread is safe and non-blocking.
    """
    if not os.path.isfile(model_path):  # disk-io: local model cache check
        return

    def _warm():
        _set_background_priority()
        try:
            get_session(model_path)
        except Exception:  # why: best-effort warmup; missing model or runtime error must not surface
            logger.debug("Prewarm failed for %s", model_path, exc_info=True)

    threading.Thread(target=_warm, daemon=True, name="onnx-prewarm").start()


def release_all():
    with _registry_lock:
        _sessions.clear()
        _model_locks.clear()
