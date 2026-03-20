"""CLIP image and text encoding via ONNX Runtime.

Qt-free, runs on worker threads. Uses cached thumbnails to avoid NAS reads.
Graceful no-op if onnxruntime or numpy is not installed.
"""
import json
import logging
import os
from typing import Optional

from core import onnx_runtime
from core.model_manager import get_model_path, ensure_model

logger = logging.getLogger(__name__)

# CLIP ViT-B-32 image preprocessing constants
_CLIP_IMAGE_SIZE = 224
_CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
_CLIP_STD = (0.26862954, 0.26130258, 0.27577711)

_tokenizer_cache = None


def _get_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def is_available() -> bool:
    return _get_numpy() is not None


def _load_and_preprocess_image(image_path: str):
    """Returns (1, 3, 224, 224) float32 array, or None."""
    np = _get_numpy()
    if np is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        img = Image.open(image_path).convert("RGB")  # disk-io: load cached thumbnail or source for CLIP encoding
    except Exception:  # why: PIL raises UnidentifiedImageError, OSError, and arbitrary decoder exceptions
        logger.debug("Failed to open image: %s", image_path, exc_info=True)
        return None

    # Resize shortest side to 224, then center crop
    w, h = img.size
    if w < h:
        new_w = _CLIP_IMAGE_SIZE
        new_h = int(h * _CLIP_IMAGE_SIZE / w)
    else:
        new_h = _CLIP_IMAGE_SIZE
        new_w = int(w * _CLIP_IMAGE_SIZE / h)
    img = img.resize((new_w, new_h), Image.BICUBIC)

    # Center crop
    left = (new_w - _CLIP_IMAGE_SIZE) // 2
    top = (new_h - _CLIP_IMAGE_SIZE) // 2
    img = img.crop((left, top, left + _CLIP_IMAGE_SIZE, top + _CLIP_IMAGE_SIZE))

    # To numpy, normalize
    arr = np.array(img, dtype=np.float32) / 255.0
    for c in range(3):
        arr[:, :, c] = (arr[:, :, c] - _CLIP_MEAN[c]) / _CLIP_STD[c]

    # HWC → CHW, add batch dim
    arr = arr.transpose(2, 0, 1)
    arr = np.expand_dims(arr, 0)
    return arr


def _load_tokenizer(config_manager=None):
    """Load and cache tokenizer.json; returns parsed dict or None."""
    global _tokenizer_cache
    if _tokenizer_cache is not None:
        return _tokenizer_cache

    path = get_model_path("clip-vit-b-32-tokenizer", config_manager)
    if not path or not os.path.isfile(path):  # disk-io: local model cache check
        path = ensure_model("clip-vit-b-32-tokenizer", config_manager)
    if not path:
        return None

    try:
        with open(path, "r") as f:  # disk-io: load tokenizer JSON from model cache
            data = json.load(f)
        _tokenizer_cache = data
        return data
    except Exception:  # why: JSON parse errors, IO errors, or corrupt tokenizer data
        logger.error("Failed to load tokenizer", exc_info=True)
        return None


def _tokenize(text: str, context_length: int = 77, config_manager=None):
    """Returns (1, context_length) int32 token array, or None."""
    np = _get_numpy()
    if np is None:
        return None

    tokenizer_data = _load_tokenizer(config_manager)
    if tokenizer_data is None:
        return None

    try:
        # Use the tokenizers library if available for proper BPE
        try:
            from tokenizers import Tokenizer
            tok_path = get_model_path("clip-vit-b-32-tokenizer", config_manager)
            tokenizer = Tokenizer.from_file(tok_path)
            encoded = tokenizer.encode(text)
            tokens = encoded.ids
        except ImportError:
            # Fallback: use the pre_tokenizer + model from tokenizer.json
            # This is a simplified path — use word-level tokens from the vocab
            vocab = tokenizer_data.get("model", {}).get("vocab", {})
            if not vocab:
                logger.error("No vocab found in tokenizer.json")
                return None
            # Simple whitespace tokenization + vocab lookup
            sot_token = 49406  # <|startoftext|>
            eot_token = 49407  # <|endoftext|>
            text_lower = text.lower()
            words = text_lower.split()
            tokens = [sot_token]
            for word in words:
                # Try exact match, then with trailing </w>
                token_id = vocab.get(word + "</w>")
                if token_id is not None:
                    tokens.append(token_id)
                else:
                    # Character-level fallback
                    for ch in word:
                        ch_id = vocab.get(ch + "</w>") or vocab.get(ch)
                        if ch_id is not None:
                            tokens.append(ch_id)
            tokens.append(eot_token)

        # Pad/truncate to context_length
        if len(tokens) > context_length:
            tokens = tokens[:context_length]
        else:
            tokens = tokens + [0] * (context_length - len(tokens))

        return np.array([tokens], dtype=np.int32)
    except Exception:  # why: tokenizers library or dict-path fallback can raise KeyError/IndexError
        logger.error("Tokenization failed", exc_info=True)
        return None


def encode_image(image_path: str, thumbnail_path: Optional[str] = None,
                 config_manager=None):
    """Prefers thumbnail_path (local cache) over image_path (may be on NAS).

    Returns L2-normalized float32 (512,) array, or None.
    """
    np = _get_numpy()
    if np is None or not onnx_runtime.is_available():
        return None

    # Use thumbnail if available to avoid NAS reads
    source = thumbnail_path if (thumbnail_path and os.path.isfile(thumbnail_path)) else image_path  # disk-io: prefer local cache over NAS

    model_path = get_model_path("clip-vit-b-32-visual", config_manager)
    if not model_path or not os.path.isfile(model_path):  # disk-io: local model cache check
        model_path = ensure_model("clip-vit-b-32-visual", config_manager)
    if not model_path:
        return None

    session = onnx_runtime.get_session_bg(model_path)
    if session is None:
        return None

    pixel_values = _load_and_preprocess_image(source)
    if pixel_values is None:
        return None

    try:
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: pixel_values})
        embedding = outputs[0].flatten().astype(np.float32)

        # L2 normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding
    except Exception:  # why: onnxruntime raises undocumented C++ exceptions on inference failure
        logger.error("CLIP image encoding failed: %s", source, exc_info=True)
        return None


def encode_text(query: str, config_manager=None):
    """Returns L2-normalized float32 (512,) array, or None."""
    np = _get_numpy()
    if np is None or not onnx_runtime.is_available():
        return None

    model_path = get_model_path("clip-vit-b-32-textual", config_manager)
    if not model_path or not os.path.isfile(model_path):  # disk-io: local model cache check
        model_path = ensure_model("clip-vit-b-32-textual", config_manager)
    if not model_path:
        return None

    session = onnx_runtime.get_session_bg(model_path)
    if session is None:
        return None

    tokens = _tokenize(query, config_manager=config_manager)
    if tokens is None:
        return None

    try:
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: tokens})
        embedding = outputs[0].flatten().astype(np.float32)

        # L2 normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding
    except Exception:  # why: onnxruntime raises undocumented C++ exceptions on inference failure
        logger.error("CLIP text encoding failed: %s", query, exc_info=True)
        return None


def embedding_to_bytes(embedding) -> bytes:
    """Raw float32 bytes for SQLite BLOB storage."""
    return embedding.tobytes()


def bytes_to_embedding(data: bytes):
    """Inverse of embedding_to_bytes."""
    np = _get_numpy()
    if np is None:
        return None
    return np.frombuffer(data, dtype=np.float32).copy()
