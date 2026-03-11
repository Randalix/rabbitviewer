"""Cosine similarity search over CLIP embeddings.

Pure-function module, no Qt or daemon dependencies (like core/heatmap.py).
"""
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


def _get_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def search(
    query_embedding,
    file_paths: List[str],
    embeddings,
    top_k: int = 50,
) -> List[Tuple[str, float]]:
    """Dot-product on L2-normalized vectors = cosine similarity.

    query_embedding: (D,) float32; embeddings: (N, D) float32.
    Returns [(file_path, score), ...] sorted descending.
    """
    np = _get_numpy()
    if np is None:
        return []
    if len(file_paths) == 0:
        return []

    # Dot product on L2-normalized vectors = cosine similarity
    scores = embeddings @ query_embedding
    top_k = min(top_k, len(file_paths))
    # argpartition is O(N) vs O(N log N) for full sort
    top_indices = np.argpartition(scores, -top_k)[-top_k:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

    return [(file_paths[i], float(scores[i])) for i in top_indices]


def build_embedding_matrix(embedding_rows: List[tuple]):
    """Returns (file_paths, (N, D) float32 matrix) or ([], None) if empty."""
    np = _get_numpy()
    if np is None or not embedding_rows:
        return [], None

    file_paths = []
    vectors = []
    for fp, blob in embedding_rows:
        vec = np.frombuffer(blob, dtype=np.float32).copy()
        file_paths.append(fp)
        vectors.append(vec)

    return file_paths, np.stack(vectors)
