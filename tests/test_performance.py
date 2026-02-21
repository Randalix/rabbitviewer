"""
Performance benchmarks for core RabbitViewer operations.

Run with:
    pytest tests/test_performance.py -v

Results are written to tests/perf_results/<iso-timestamp>.json.
If tests/perf_results/baseline.json exists, each benchmark is compared
against it and a warning is emitted for regressions > 20 %.

To lock in the current run as the new baseline:
    cp tests/perf_results/<latest>.json tests/perf_results/baseline.json
"""
import json
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PERF_DIR = Path(__file__).parent / "perf_results"
BASELINE_PATH = PERF_DIR / "baseline.json"
REGRESSION_THRESHOLD = 0.20  # warn if mean_ms is > 20 % slower than baseline


def _bench(fn: Callable, iterations: int = 100) -> dict:
    """Run *fn* for *iterations* and return timing stats (ms)."""
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - t0) * 1000)
    return {
        "iterations": iterations,
        "total_ms": sum(timings),
        "mean_ms": sum(timings) / len(timings),
        "min_ms": min(timings),
        "max_ms": max(timings),
    }


class PerfTracker:
    """Collects benchmark results and persists them at session end."""

    def __init__(self):
        self.results: dict[str, dict] = {}
        self.baseline: dict[str, dict] = self._load_baseline()

    def _load_baseline(self) -> dict:
        if BASELINE_PATH.exists():
            return json.loads(BASELINE_PATH.read_text())
        return {}

    def record(self, name: str, stats: dict):
        self.results[name] = stats
        if name in self.baseline:
            base_mean = self.baseline[name]["mean_ms"]
            delta = (stats["mean_ms"] - base_mean) / base_mean
            if delta > REGRESSION_THRESHOLD:
                warnings.warn(
                    f"PERF REGRESSION [{name}]: "
                    f"{stats['mean_ms']:.3f} ms vs baseline {base_mean:.3f} ms "
                    f"(+{delta * 100:.1f} %)"
                )

    def save(self):
        PERF_DIR.mkdir(exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = PERF_DIR / f"{ts}.json"
        out_path.write_text(json.dumps(self.results, indent=2))
        return out_path


@pytest.fixture(scope="module")
def perf_tracker():
    tracker = PerfTracker()
    yield tracker
    saved = tracker.save()
    print(f"\n[perf] Results saved to {saved}")


@pytest.fixture(scope="module")
def plugin_env(tmp_path_factory):
    """PILPlugin instance with a 1920x1080 source image in a temp cache dir."""
    from plugins.pil_plugin import PILPlugin

    tmp = tmp_path_factory.mktemp("plugin_bench")
    src_path = str(tmp / "source.jpg")
    Image.new("RGB", (1920, 1080), color=(128, 64, 32)).save(src_path, "JPEG", quality=95)

    plugin = PILPlugin(cache_dir=str(tmp), thumbnail_size=256)
    return plugin, src_path, tmp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_env(tmp_path):
    """Fresh MetadataDatabase in a temp directory — no global singleton."""
    import core.metadata_database as _mdb_module
    from core.metadata_database import MetadataDatabase

    _mdb_module._metadata_database = None
    db_path = str(tmp_path / "metadata.db")
    db = MetadataDatabase(db_path)
    yield db, tmp_path
    db.close()
    _mdb_module._metadata_database = None


@pytest.fixture()
def populated_db(db_env):
    """Database pre-loaded with 200 image records."""
    db, tmp_path = db_env
    img_dir = tmp_path / "images"
    img_dir.mkdir()

    paths: list[str] = []
    for i in range(200):
        p = str(img_dir / f"image_{i:04d}.jpg")
        # Write a minimal JPEG so os.stat works
        Image.new("RGB", (4, 4), color=(i % 255, 0, 0)).save(p, "JPEG")
        paths.append(p)

    db.batch_ensure_records_exist(paths)
    return db, paths


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

class TestDatabasePerformance:
    """Benchmarks for MetadataDatabase operations."""

    def test_single_record_insert(self, db_env, perf_tracker):
        """Time batch_ensure_records_exist with a single path."""
        db, tmp_path = db_env
        img = tmp_path / "single.jpg"
        Image.new("RGB", (4, 4)).save(str(img), "JPEG")
        path = str(img)

        stats = _bench(lambda: db.batch_ensure_records_exist([path]))
        perf_tracker.record("db.single_record_insert", stats)

        print(f"\n  single insert: {stats['mean_ms']:.3f} ms mean over {stats['iterations']} runs")
        assert stats["mean_ms"] < 50, f"Single insert too slow: {stats['mean_ms']:.2f} ms"

    def test_batch_insert_100(self, db_env, perf_tracker):
        """Time inserting 100 records in one batch_ensure_records_exist call."""
        db, base = db_env
        img_dir = base / "batch"
        img_dir.mkdir()
        paths = []
        for i in range(100):
            p = str(img_dir / f"b{i:03d}.jpg")
            Image.new("RGB", (4, 4), color=(i, 0, 0)).save(p, "JPEG")
            paths.append(p)

        stats = _bench(lambda: db.batch_ensure_records_exist(paths), iterations=20)
        perf_tracker.record("db.batch_insert_100", stats)

        print(f"\n  batch insert 100: {stats['mean_ms']:.3f} ms mean over {stats['iterations']} runs")
        assert stats["mean_ms"] < 500, f"Batch insert too slow: {stats['mean_ms']:.2f} ms"

    def test_get_metadata_miss(self, db_env, perf_tracker):
        """Time get_metadata() for a path not in the database."""
        db, tmp_path = db_env
        missing = str(tmp_path / "nonexistent.jpg")

        stats = _bench(lambda: db.get_metadata(missing))
        perf_tracker.record("db.get_metadata_miss", stats)

        print(f"\n  get_metadata miss: {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 10

    def test_get_metadata_hit(self, populated_db, perf_tracker):
        """Time get_metadata() for a record that exists."""
        db, paths = populated_db
        target = paths[0]

        stats = _bench(lambda: db.get_metadata(target))
        perf_tracker.record("db.get_metadata_hit", stats)

        print(f"\n  get_metadata hit: {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 10

    def test_is_thumbnail_valid_no_thumb(self, populated_db, perf_tracker):
        """Time is_thumbnail_valid() when no thumbnail path is recorded."""
        db, paths = populated_db
        target = paths[10]

        stats = _bench(lambda: db.is_thumbnail_valid(target))
        perf_tracker.record("db.is_thumbnail_valid_no_thumb", stats)

        print(f"\n  is_thumbnail_valid (no thumb): {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 10

    def test_get_filtered_file_paths_no_filter(self, populated_db, perf_tracker):
        """Time get_filtered_file_paths() with no filter active (returns all paths)."""
        db, paths = populated_db
        # star_states: list of 6 booleans (indices 0–5 = unrated through 5-star)
        all_stars = [True] * 6

        stats = _bench(
            lambda: db.get_filtered_file_paths(text_filter="", star_states=all_stars),
            iterations=50,
        )
        perf_tracker.record("db.get_filtered_file_paths_none", stats)

        result = db.get_filtered_file_paths(text_filter="", star_states=all_stars)
        print(f"\n  get_filtered (no filter, {len(result)} rows): {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 50

    def test_get_filtered_file_paths_text(self, populated_db, perf_tracker):
        """Time get_filtered_file_paths() with a text filter."""
        db, paths = populated_db
        all_stars = [True] * 6

        stats = _bench(
            lambda: db.get_filtered_file_paths(text_filter="image_01", star_states=all_stars),
            iterations=50,
        )
        perf_tracker.record("db.get_filtered_file_paths_text", stats)

        print(f"\n  get_filtered (text): {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 50

    def test_rating_update(self, populated_db, perf_tracker):
        """Time set_rating() for a single record."""
        db, paths = populated_db
        target = paths[5]

        call = [0]

        def toggle():
            db.set_rating(target, call[0] % 5 + 1)
            call[0] += 1

        stats = _bench(toggle)
        perf_tracker.record("db.set_rating", stats)

        print(f"\n  set_rating: {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 20

    def test_batch_rating_update_50(self, populated_db, perf_tracker):
        """Time batch_set_ratings() across 50 records at rating=3."""
        db, paths = populated_db
        batch = paths[:50]

        stats = _bench(lambda: db.batch_set_ratings(batch, 3), iterations=20)
        perf_tracker.record("db.batch_set_ratings_50", stats)

        print(f"\n  batch_set_ratings 50: {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 200


class TestPluginPerformance:
    """Benchmarks for plugin registry and PIL image processing."""

    def test_registry_lookup(self, perf_tracker):
        """Time format→plugin dict lookup in the registry."""
        from plugins.base_plugin import plugin_registry

        stats = _bench(lambda: plugin_registry.get_plugin_for_format(".jpg"), iterations=1000)
        perf_tracker.record("plugin.registry_lookup_jpg", stats)

        print(f"\n  registry lookup .jpg: {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 1

    def test_pil_generate_thumbnail(self, plugin_env, perf_tracker):
        """Time generate_thumbnail(): PIL open 1920×1080 JPEG, resize to 256px, save."""
        plugin, src_path, tmp = plugin_env
        out = str(tmp / "bench_thumb_out.jpg")

        stats = _bench(
            lambda: plugin.generate_thumbnail(src_path, image_source=src_path, orientation=1, output_path=out),
            iterations=20,
        )
        perf_tracker.record("plugin.pil_generate_thumbnail", stats)

        print(f"\n  pil generate_thumbnail (1920×1080→256px): {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 500

    def test_pil_generate_view_image(self, plugin_env, perf_tracker):
        """Time generate_view_image(): PIL open 1920×1080 JPEG, convert, save at quality=95."""
        plugin, src_path, tmp = plugin_env
        out = str(tmp / "bench_view_out.jpg")

        stats = _bench(
            lambda: plugin.generate_view_image(src_path, image_source=src_path, orientation=1, output_path=out),
            iterations=20,
        )
        perf_tracker.record("plugin.pil_generate_view_image", stats)

        print(f"\n  pil generate_view_image (1920×1080): {stats['mean_ms']:.3f} ms mean")
        assert stats["mean_ms"] < 500

    def test_pil_process_thumbnail_hit(self, plugin_env, perf_tracker):
        """Time process_thumbnail() for a warm cache hit (os.path.exists early return)."""
        plugin, src_path, _ = plugin_env
        plugin.process_thumbnail(src_path, md5_hash="bench_hit_thumb")  # warm

        stats = _bench(
            lambda: plugin.process_thumbnail(src_path, md5_hash="bench_hit_thumb"),
            iterations=200,
        )
        perf_tracker.record("plugin.pil_process_thumbnail_hit", stats)

        print(f"\n  pil process_thumbnail (cache hit): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 5

    def test_pil_process_view_image_hit(self, plugin_env, perf_tracker):
        """Time process_view_image() for a warm cache hit (os.path.exists early return)."""
        plugin, src_path, _ = plugin_env
        plugin.process_view_image(src_path, md5_hash="bench_hit_view")  # warm

        stats = _bench(
            lambda: plugin.process_view_image(src_path, md5_hash="bench_hit_view"),
            iterations=200,
        )
        perf_tracker.record("plugin.pil_process_view_image_hit", stats)

        print(f"\n  pil process_view_image (cache hit): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 5
