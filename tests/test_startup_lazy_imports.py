"""Tests that startup-critical imports stay lazy and deferred init works correctly.

These tests guard against regressions that would pull heavy modules back into
the critical path before the window's first paint.
"""
import subprocess
import sys
import textwrap

import pytest


def _run_import_check(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a subprocess using the same interpreter."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True, text=True, timeout=30,
    )


_pyside6_available = subprocess.run(
    [sys.executable, "-c", "import PySide6"],
    capture_output=True,
).returncode == 0

# Guard: skip the whole module when PySide6 is not installed (CI / stub-only env).
pytestmark = pytest.mark.skipif(
    not _pyside6_available,
    reason="PySide6 not installed — lazy-import tests require the real package",
)


class TestLazyImports:
    """Importing gui.main_window must NOT eagerly import heavy view modules."""

    def test_picture_view_not_imported(self):
        r = _run_import_check("""
            import sys, gui.main_window
            assert "gui.picture_view" not in sys.modules, (
                "gui.picture_view was imported at module level"
            )
        """)
        assert r.returncode == 0, r.stderr

    def test_inspector_view_not_imported(self):
        r = _run_import_check("""
            import sys, gui.main_window
            assert "gui.inspector_view" not in sys.modules, (
                "gui.inspector_view was imported at module level"
            )
        """)
        assert r.returncode == 0, r.stderr

    def test_status_bar_not_imported(self):
        r = _run_import_check("""
            import sys, gui.main_window
            assert "gui.status_bar" not in sys.modules, (
                "gui.status_bar was imported at module level"
            )
        """)
        assert r.returncode == 0, r.stderr

    def test_video_plugin_not_imported(self):
        r = _run_import_check("""
            import sys, gui.main_window
            assert "plugins.video_plugin" not in sys.modules, (
                "plugins.video_plugin was imported at module level"
            )
        """)
        assert r.returncode == 0, r.stderr


class TestVideoExtensionsConstant:
    """The inlined _VIDEO_EXTENSIONS in main_window must match the plugin's canonical list."""

    def test_matches_video_plugin(self):
        r = _run_import_check("""
            from gui.main_window import _VIDEO_EXTENSIONS
            from plugins.video_plugin import VIDEO_EXTENSIONS
            assert _VIDEO_EXTENSIONS == frozenset(VIDEO_EXTENSIONS), (
                f"Mismatch: main_window has {_VIDEO_EXTENSIONS!r}, "
                f"video_plugin has {frozenset(VIDEO_EXTENSIONS)!r}"
            )
        """)
        assert r.returncode == 0, r.stderr
