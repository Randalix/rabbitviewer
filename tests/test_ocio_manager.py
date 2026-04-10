"""Tests for OCIO assignment storage, resolution, and transform."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.db.ocio_table import OcioAssignment
from core.ocio_manager import OcioManager


# ---------------------------------------------------------------------------
# OcioTable — DB layer
# ---------------------------------------------------------------------------


class TestOcioTable:

    def test_set_and_get_file_assignment(self, tmp_env):
        db = tmp_env["db"]
        db.ocio.set("/img/photo.jpg", False, "/configs/aces.ocio", "sRGB")
        result = db.ocio.get_for_file("/img/photo.jpg")
        assert result is not None
        assert result.config_path == "/configs/aces.ocio"
        assert result.input_space == "sRGB"
        assert result.is_folder is False

    def test_file_assignment_exact_match_wins_over_folder(self, tmp_env):
        db = tmp_env["db"]
        # Folder assignment
        db.ocio.set("/img", True, "/configs/folder.ocio", "ACEScg")
        # File-level override
        db.ocio.set("/img/photo.jpg", False, "/configs/file.ocio", "sRGB")

        result = db.ocio.get_for_file("/img/photo.jpg")
        assert result is not None
        assert result.config_path == "/configs/file.ocio"

    def test_folder_inheritance_longest_match(self, tmp_env):
        db = tmp_env["db"]
        # Two ancestor assignments — nearest one should win
        db.ocio.set("/img", True, "/configs/root.ocio", "sRGB")
        db.ocio.set("/img/raw", True, "/configs/raw.ocio", "ACEScg")

        result = db.ocio.get_for_file("/img/raw/shot.cr3")
        assert result is not None
        assert result.config_path == "/configs/raw.ocio"  # nearest wins

    def test_no_assignment_returns_none(self, tmp_env):
        db = tmp_env["db"]
        result = db.ocio.get_for_file("/no/assignment/here.jpg")
        assert result is None

    def test_delete_removes_assignment(self, tmp_env):
        db = tmp_env["db"]
        db.ocio.set("/img/photo.jpg", False, "/configs/a.ocio", "sRGB")
        db.ocio.delete("/img/photo.jpg")
        assert db.ocio.get_for_file("/img/photo.jpg") is None

    def test_upsert_updates_existing(self, tmp_env):
        db = tmp_env["db"]
        db.ocio.set("/img/photo.jpg", False, "/configs/a.ocio", "sRGB")
        db.ocio.set("/img/photo.jpg", False, "/configs/b.ocio", "ACEScg")
        result = db.ocio.get_for_file("/img/photo.jpg")
        assert result.config_path == "/configs/b.ocio"
        assert result.input_space == "ACEScg"

    def test_get_all(self, tmp_env):
        db = tmp_env["db"]
        db.ocio.set("/a.jpg", False, "/cfg.ocio", "sRGB")
        db.ocio.set("/folder", True, "/cfg.ocio", "ACEScg")
        all_rows = db.ocio.get_all()
        assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# OcioManager — resolution and transform (no PyOpenColorIO required)
# ---------------------------------------------------------------------------


class TestOcioManager:

    def test_no_db_returns_none(self):
        mgr = OcioManager()
        # db not initialised — should return None without crashing
        result = mgr.get_assignment("/any/file.jpg")
        assert result is None

    def test_available_reflects_import(self):
        mgr = OcioManager()
        try:
            import PyOpenColorIO as _ocio  # noqa: F401
            assert mgr.available is True
        except ImportError:
            assert mgr.available is False

    def test_apply_noop_without_pyocio(self):
        """apply() returns the original image unchanged when PyOpenColorIO is absent."""
        mgr = OcioManager()
        import core.ocio_manager as _mod
        orig = _mod._OCIO_AVAILABLE
        _mod._OCIO_AVAILABLE = False
        try:
            from PIL import Image
            img = Image.new("RGB", (8, 8), (128, 64, 32))
            assignment = OcioAssignment(
                path="/folder",
                is_folder=True,
                config_path="/nonexistent.ocio",
                input_space="sRGB",
            )
            result = mgr.apply(img, assignment)
            assert result is img  # unchanged reference
        finally:
            _mod._OCIO_AVAILABLE = orig

    def test_manager_init_attaches_db(self, tmp_env):
        db = tmp_env["db"]
        mgr = OcioManager()
        mgr.init(db)
        assert mgr._db is db

    def test_get_assignment_queries_db(self, tmp_env):
        db = tmp_env["db"]
        db.ocio.set("/photos", True, "/cfg/aces.ocio", "sRGB")
        mgr = OcioManager()
        mgr.init(db)
        result = mgr.get_assignment("/photos/img.jpg")
        if mgr.available:
            # If PyOpenColorIO installed, we'd get a result
            assert result is not None
        else:
            # Without PyOpenColorIO, get_assignment short-circuits to None
            assert result is None
