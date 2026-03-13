"""Face clustering via cosine similarity.

Pure-function module (like core/clip_search.py). No Qt or daemon dependencies.
"""
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _get_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def compute_person_mean(embeddings):
    """Compute mean of L2-normalized face embeddings, then re-normalize."""
    np = _get_numpy()
    if np is None or not embeddings:
        return None

    stacked = np.stack(embeddings)
    mean = stacked.mean(axis=0)
    norm = np.linalg.norm(mean)
    if norm > 0:
        mean = mean / norm
    return mean.astype(np.float32)


def assign_faces(
    new_faces: List[Tuple[str, object]],
    person_means: List[Tuple[str, object]],
    threshold: float = 0.6,
) -> List[Tuple[str, Optional[str]]]:
    """Match face embeddings against existing person mean embeddings.

    Returns [(face_id, matched_person_id_or_None), ...]
    """
    np = _get_numpy()
    if np is None:
        return [(fid, None) for fid, _ in new_faces]

    if not person_means:
        return [(fid, None) for fid, _ in new_faces]

    person_ids = [pid for pid, _ in person_means]
    person_matrix = np.stack([emb for _, emb in person_means])

    results = []
    for face_id, embedding in new_faces:
        # Cosine similarity (embeddings are L2-normalized)
        scores = person_matrix @ embedding
        best_idx = int(np.argmax(scores))
        best_score = float(scores[best_idx])

        if best_score >= threshold:
            results.append((face_id, person_ids[best_idx]))
        else:
            results.append((face_id, None))

    return results
