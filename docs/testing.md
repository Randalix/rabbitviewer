# Testing

## Running the test suite

```bash
venv/bin/pytest tests/ -v
```

Run only performance benchmarks:

```bash
venv/bin/pytest tests/test_performance.py -v -s
```

The `-s` flag lets benchmark timing print inline during the run.

---

## Test infrastructure

### `tests/conftest.py`

Shared fixtures available to every test module.

#### `tmp_env` fixture

Provides a fully isolated environment backed by a `tmp_path` directory that pytest creates and removes per test. Resets the global `MetadataDatabase` singleton before and after each test so no database state leaks between tests.

Yields a dict:

| Key | Type | Description |
|-----|------|-------------|
| `tmp_path` | `pathlib.Path` | Root temp directory |
| `cache_dir` | `pathlib.Path` | `tmp_path/cache/` |
| `db_path` | `str` | Path to the fresh SQLite database |
| `db` | `MetadataDatabase` | Open database instance |
| `config` | `MockConfigManager` | Config substitute wired to this environment |

#### `MockConfigManager`

A dict-backed substitute for `ConfigManager` that requires no YAML file. Accepts an `overrides` dict at construction. Defaults:

| Key | Default | Notes |
|-----|---------|-------|
| `thumbnail_size` | `128` | |
| `min_file_size` | `0` | Accepts all file sizes so small test images pass |
| `ignore_patterns` | `[]` | |
| `cache_dir` | `None` | Set by `tmp_env` to the temp cache path |

#### `sample_images` fixture

Depends on `tmp_env`. Creates 20 small JPEG files (`image_0000.jpg` … `image_0019.jpg`) inside `tmp_path/images/` and returns their paths as `list[str]`.

---

## Performance benchmarks

### How they work

Each benchmark in `tests/test_performance.py` calls `_bench(fn, iterations)`, which:
1. Runs `fn()` for the requested number of iterations
2. Records wall-clock time per call with `time.perf_counter()`
3. Returns `{iterations, total_ms, mean_ms, min_ms, max_ms}`

Results are written to `tests/perf_results/<iso-timestamp>Z.json` at the end of the test session. Timestamped result files are gitignored; only `baseline.json` is tracked.

### Regression detection

If `tests/perf_results/baseline.json` exists, each benchmark compares its `mean_ms` against the baseline. A regression of more than 20 % triggers `warnings.warn`, visible in pytest output.

### Setting a new baseline

After a run whose results you want to lock in:

```bash
cp tests/perf_results/<timestamp>Z.json tests/perf_results/baseline.json
git add tests/perf_results/baseline.json
git commit -m "perf: update baseline"
```

### Current baseline

| Benchmark | mean (ms) |
|-----------|----------:|
| `db.single_record_insert` | 0.005 |
| `db.batch_insert_100` | 0.131 |
| `db.get_metadata_miss` | 0.002 |
| `db.get_metadata_hit` | 0.010 |
| `db.is_thumbnail_valid_no_thumb` | 0.005 |
| `db.get_filtered_file_paths_none` | 0.069 |
| `db.get_filtered_file_paths_text` | 0.076 |
| `db.set_rating` | 0.087 |
| `db.batch_set_ratings_50` | 0.348 |

### Adding a benchmark

Add a method to `TestDatabasePerformance` in `tests/test_performance.py`:

```python
def test_my_operation(self, populated_db, perf_tracker):
    db, paths = populated_db
    stats = _bench(lambda: db.some_method(paths[0]))
    perf_tracker.record("db.my_operation", stats)
    assert stats["mean_ms"] < 20
