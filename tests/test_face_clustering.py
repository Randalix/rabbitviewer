"""Tests for core/face_clustering.py."""
import numpy as np
from core.face_clustering import assign_faces, compute_person_mean


def _random_embedding(seed=0):
    rng = np.random.RandomState(seed)
    v = rng.randn(512).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


class TestComputePersonMean:
    def test_single_embedding(self):
        emb = _random_embedding(0)
        mean = compute_person_mean([emb])
        assert mean is not None
        # Mean of a single L2-normalized vector should be the vector itself
        assert np.allclose(mean, emb, atol=1e-5)

    def test_multiple_embeddings(self):
        e1 = _random_embedding(1)
        e2 = _random_embedding(2)
        mean = compute_person_mean([e1, e2])
        assert mean is not None
        # Result should be L2-normalized
        assert abs(np.linalg.norm(mean) - 1.0) < 1e-5

    def test_empty_returns_none(self):
        assert compute_person_mean([]) is None


class TestAssignFaces:
    def test_match_above_threshold(self):
        emb = _random_embedding(10)
        # Person mean is very close to the face
        person_mean = emb.copy()
        person_mean[0] += 0.001
        person_mean /= np.linalg.norm(person_mean)

        results = assign_faces(
            [("face1", emb)],
            [("person1", person_mean)],
            threshold=0.5,
        )
        assert len(results) == 1
        assert results[0] == ("face1", "person1")

    def test_no_match_below_threshold(self):
        e1 = _random_embedding(20)
        e2 = _random_embedding(30)  # seeds 20 vs 30 in 512D → cosine sim ≈ 0.0

        results = assign_faces(
            [("face1", e1)],
            [("person1", e2)],
            threshold=0.99,  # very high threshold
        )
        assert len(results) == 1
        assert results[0] == ("face1", None)

    def test_empty_persons(self):
        emb = _random_embedding(40)
        results = assign_faces([("face1", emb)], [], threshold=0.5)
        assert len(results) == 1
        assert results[0] == ("face1", None)

    def test_empty_faces(self):
        person_mean = _random_embedding(50)
        results = assign_faces([], [("person1", person_mean)], threshold=0.5)
        assert len(results) == 0

    def test_multiple_persons_best_match(self):
        emb = _random_embedding(60)
        # Create two persons, one close and one far
        close = emb.copy()
        close[0] += 0.01
        close /= np.linalg.norm(close)

        far = _random_embedding(70)

        results = assign_faces(
            [("face1", emb)],
            [("far_person", far), ("close_person", close)],
            threshold=0.5,
        )
        assert len(results) == 1
        assert results[0][1] == "close_person"
