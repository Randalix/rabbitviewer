"""
Tests for the rating system: DB batch operations, plugin validation,
config hotkeys, rating scripts, filter logic, and StarDragContext.
"""
import importlib
import os
import shutil
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from plugins.pil_plugin import PILPlugin
from config.config_manager import DEFAULT_CONFIG


def _exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


@pytest.fixture()
def pil_plugin(tmp_path):
    return PILPlugin(cache_dir=str(tmp_path / "cache"))


@pytest.fixture()
def jpeg_file(tmp_path):
    path = tmp_path / "test.jpg"
    Image.new("RGB", (64, 64), color=(128, 64, 32)).save(str(path), "JPEG")
    return str(path)


# ---------------------------------------------------------------------------
# 1. batch_set_ratings — DB layer
# ---------------------------------------------------------------------------


class TestBatchSetRatings:

    def test_existing_records(self, tmp_env, sample_images):
        db = tmp_env["db"]
        # Pre-populate with rating 0
        db.batch_set_ratings(sample_images[:5], 0)
        # Now set rating 3
        ok, count = db.batch_set_ratings(sample_images[:5], 3)
        assert ok is True
        assert count == 5
        for p in sample_images[:5]:
            assert db.get_rating(p) == 3

    def test_new_records(self, tmp_env, sample_images):
        db = tmp_env["db"]
        ok, count = db.batch_set_ratings(sample_images[:3], 4)
        assert ok is True
        assert count == 3
        for p in sample_images[:3]:
            assert db.get_rating(p) == 4

    def test_mixed(self, tmp_env, sample_images):
        db = tmp_env["db"]
        db.batch_set_ratings(sample_images[:2], 1)
        ok, count = db.batch_set_ratings(sample_images[:5], 2)
        assert ok is True
        assert count == 5
        for p in sample_images[:5]:
            assert db.get_rating(p) == 2

    def test_returns_tuple(self, tmp_env, sample_images):
        db = tmp_env["db"]
        result = db.batch_set_ratings(sample_images[:1], 1)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_empty_list(self, tmp_env):
        db = tmp_env["db"]
        result = db.batch_set_ratings([], 3)
        assert result == (True, 0)

    def test_missing_files(self, tmp_env, sample_images):
        db = tmp_env["db"]
        missing = ["/nonexistent/image_9999.jpg"]
        paths = sample_images[:2] + missing
        ok, count = db.batch_set_ratings(paths, 5)
        assert ok is False
        assert count == 2

    def test_all_missing(self, tmp_env):
        db = tmp_env["db"]
        paths = ["/nonexistent/a.jpg", "/nonexistent/b.jpg"]
        ok, count = db.batch_set_ratings(paths, 1)
        assert ok is False
        assert count == 0


# ---------------------------------------------------------------------------
# 2. write_rating validation — plugin layer
# ---------------------------------------------------------------------------


class TestWriteRatingValidation:

    def test_rejects_negative(self, pil_plugin, jpeg_file):
        assert pil_plugin.write_rating(jpeg_file, -1) is False

    def test_rejects_above_five(self, pil_plugin, jpeg_file):
        assert pil_plugin.write_rating(jpeg_file, 6) is False

    @pytest.mark.skipif(not _exiftool_available(), reason="exiftool not installed")
    @pytest.mark.parametrize("rating", [0, 5])
    def test_accepts_boundary_values(self, pil_plugin, jpeg_file, rating):
        assert pil_plugin.write_rating(jpeg_file, rating) is True


# ---------------------------------------------------------------------------
# 3. Config hotkeys
# ---------------------------------------------------------------------------


class TestConfigHotkeys:

    _rating_keys = [f"script:set_rating_{n}" for n in range(6)]

    def test_rating_hotkeys_present(self):
        hotkeys = DEFAULT_CONFIG["hotkeys"]
        for key in self._rating_keys:
            assert key in hotkeys, f"Missing hotkey: {key}"

    def test_rating_hotkeys_sequences(self):
        hotkeys = DEFAULT_CONFIG["hotkeys"]
        for n in range(6):
            assert hotkeys[f"script:set_rating_{n}"]["sequence"] == str(n)

    def test_no_duplicate_sequences(self):
        hotkeys = DEFAULT_CONFIG["hotkeys"]
        seen: dict[str, str] = {}
        for name, entry in hotkeys.items():
            seq = entry["sequence"]
            assert seq not in seen, (
                f"Duplicate sequence '{seq}': {seen[seq]} and {name}"
            )
            seen[seq] = name


# ---------------------------------------------------------------------------
# 4. Rating scripts
# ---------------------------------------------------------------------------


class TestRatingScripts:

    _scripts_dir = os.path.join(
        os.path.dirname(__file__), "..", "scripts"
    )

    @pytest.mark.parametrize("n", range(6))
    def test_all_rating_scripts_exist(self, n):
        path = os.path.join(self._scripts_dir, f"set_rating_{n}.py")
        assert os.path.isfile(path), f"Missing script: set_rating_{n}.py"

    @pytest.mark.parametrize("n", range(6))
    def test_rating_scripts_callable(self, n):
        mod = importlib.import_module(f"scripts.set_rating_{n}")
        assert callable(getattr(mod, "run_script", None))


# ---------------------------------------------------------------------------
# 5. filter_affects_rating logic
# ---------------------------------------------------------------------------


class TestFilterAffectsRating:

    @staticmethod
    def _filter_affects(star_filter):
        return not all(star_filter)

    def test_all_true_returns_false(self):
        assert self._filter_affects([True] * 6) is False

    def test_one_disabled_returns_true(self):
        f = [True] * 6
        f[3] = False
        assert self._filter_affects(f) is True


# ---------------------------------------------------------------------------
# 6. StarDragContext isolation
# ---------------------------------------------------------------------------


class TestStarDragContext:

    def test_defaults(self):
        from gui.components.star_button import StarDragContext

        ctx = StarDragContext()
        assert ctx.is_active is False
        assert ctx.initial_state is False
        assert ctx.last_button is None

    def test_independent_instances(self):
        from gui.components.star_button import StarDragContext

        a, b = StarDragContext(), StarDragContext()
        a.is_active = True
        a.last_button = 42
        assert b.is_active is False
        assert b.last_button is None
