"""Tests for core/clip_search.py — cosine similarity search."""
import pytest

numpy = pytest.importorskip("numpy")


from core.clip_search import search, build_embedding_matrix


def _make_vec(dim=512, seed=0):
    """Create a deterministic L2-normalized random vector."""
    rng = numpy.random.RandomState(seed)
    v = rng.randn(dim).astype(numpy.float32)
    v /= numpy.linalg.norm(v)
    return v


class TestSearch:
    def test_identical_vectors_score_one(self):
        q = _make_vec(seed=42)
        paths = ["a.jpg"]
        matrix = q.reshape(1, -1)
        results = search(q, paths, matrix, top_k=1)
        assert len(results) == 1
        assert results[0][0] == "a.jpg"
        assert abs(results[0][1] - 1.0) < 1e-5

    def test_orthogonal_vectors_score_zero(self):
        # Create two orthogonal vectors in 2D for clarity
        q = numpy.array([1.0, 0.0], dtype=numpy.float32)
        other = numpy.array([0.0, 1.0], dtype=numpy.float32)
        results = search(q, ["b.jpg"], other.reshape(1, -1), top_k=1)
        assert abs(results[0][1]) < 1e-5

    def test_top_k_ordering(self):
        q = _make_vec(seed=0)
        paths = [f"img_{i}.jpg" for i in range(100)]
        vecs = numpy.stack([_make_vec(seed=i) for i in range(100)])

        results = search(q, paths, vecs, top_k=5)
        assert len(results) == 5
        # Scores should be descending
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_clamped_to_n(self):
        q = _make_vec(seed=0)
        paths = ["a.jpg", "b.jpg"]
        vecs = numpy.stack([_make_vec(seed=1), _make_vec(seed=2)])
        results = search(q, paths, vecs, top_k=100)
        assert len(results) == 2

    def test_empty_returns_empty(self):
        q = _make_vec(seed=0)
        results = search(q, [], numpy.zeros((0, 512), dtype=numpy.float32), top_k=10)
        assert results == []

    def test_single_element(self):
        q = _make_vec(seed=5)
        v = _make_vec(seed=5)
        results = search(q, ["only.jpg"], v.reshape(1, -1), top_k=10)
        assert len(results) == 1
        assert results[0][0] == "only.jpg"


class TestBuildEmbeddingMatrix:
    def test_round_trip(self):
        vecs = [_make_vec(seed=i) for i in range(5)]
        rows = [(f"img_{i}.jpg", v.tobytes()) for i, v in enumerate(vecs)]
        paths, matrix = build_embedding_matrix(rows)
        assert len(paths) == 5
        assert matrix.shape == (5, 512)
        # Verify data integrity
        numpy.testing.assert_allclose(matrix[0], vecs[0], atol=1e-7)

    def test_empty(self):
        paths, matrix = build_embedding_matrix([])
        assert paths == []
        assert matrix is None
