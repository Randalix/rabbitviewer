"""
Smoke tests for bench_sessions.py — verifies the log parser returns
well-formed Session objects without crashing.
"""
import os
import textwrap
from datetime import datetime

import pytest

from benchmarks.bench_sessions import parse_gui_log, annotate_dropped_notifs

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GUI_LOG_SNIPPET = textwrap.dedent("""\
    2026-02-20 10:00:00,000 [INFO] root - Starting RabbitViewer GUI
    2026-02-20 10:00:00,800 [INFO] root - Notification listener thread started.
    2026-02-20 10:00:00,850 [INFO] root - Notification client connected to daemon.
    2026-02-20 10:00:01,000 [INFO] root - MainWindow: ThumbnailViewWidget created, connecting signals...
    2026-02-20 10:00:01,100 [INFO] root - MainWindow: Starting to load directory: /tmp/pics (Recursive: True)
    2026-02-20 10:00:01,150 [INFO] root - Daemon acknowledged scan request for /tmp/pics. Waiting for progress notifications.
    2026-02-20 10:00:01,400 [INFO] root - ThumbnailViewWidget received notification: Previews ready for /tmp/pics/a.jpg
    2026-02-20 10:00:01,420 [INFO] root - ThumbnailViewWidget received notification: Previews ready for /tmp/pics/b.jpg
    2026-02-20 10:00:01,440 [INFO] root - ThumbnailViewWidget received notification: Previews ready for /tmp/pics/c.jpg
    2026-02-20 10:00:05,000 [INFO] root - GuiServer stopped.
    2026-02-20 10:00:05,050 [INFO] root - Application exiting with code 0
""")

_DAEMON_LOG_SNIPPET = textwrap.dedent("""\
    2026-02-20 10:00:01,410 - core.thumbnail_manager - WARNING - Notification queue full; dropping previews_ready notification for /tmp/pics/x.jpg
    2026-02-20 10:00:01,430 - core.thumbnail_manager - WARNING - Notification queue full; dropping previews_ready notification for /tmp/pics/y.jpg
""")


@pytest.fixture
def gui_log(tmp_path):
    p = tmp_path / "image_viewer.log"
    p.write_text(_GUI_LOG_SNIPPET)
    return str(p)


@pytest.fixture
def daemon_log(tmp_path):
    p = tmp_path / "daemon.log"
    p.write_text(_DAEMON_LOG_SNIPPET)
    return str(p)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_parse_returns_one_session(gui_log):
    sessions = parse_gui_log(gui_log)
    assert len(sessions) == 1


def test_session_start_timestamp(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert s.t_start == datetime(2026, 2, 20, 10, 0, 0, 0)


def test_session_directory_and_recursive(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert s.directory == "/tmp/pics"
    assert s.recursive is True


def test_scan_timestamps_ordered(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert s.t_scan is not None
    assert s.t_scan_ack is not None
    assert s.t_start < s.t_scan < s.t_scan_ack


def test_thumbnail_count(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert len(s.thumb_times) == 3


def test_thumbnails_after_scan(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert all(t > s.t_scan for t in s.thumb_times)


def test_exit_code(gui_log):
    s = parse_gui_log(gui_log)[0]
    assert s.exit_code == 0


def test_dropped_notifs_counted(gui_log, daemon_log):
    sessions = parse_gui_log(gui_log)
    annotate_dropped_notifs(sessions, daemon_log)
    assert sessions[0].dropped_notifs == 2


def test_no_sessions_in_empty_log(tmp_path):
    p = tmp_path / "empty.log"
    p.write_text("")
    assert parse_gui_log(str(p)) == []


def test_missing_daemon_log_does_not_raise(gui_log, tmp_path):
    sessions = parse_gui_log(gui_log)
    annotate_dropped_notifs(sessions, str(tmp_path / "nonexistent.log"))
    assert sessions[0].dropped_notifs == 0
