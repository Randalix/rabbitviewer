"""Tests for daemon PID lock, cli/daemon.py, and cli/stop.py."""

import errno
import fcntl
import os
import signal
import subprocess
import sys
import time

import pytest


# ---------------------------------------------------------------------------
# _acquire_daemon_lock / _daemon_pid_path
# ---------------------------------------------------------------------------

class TestDaemonLock:
    def test_acquires_lock_and_writes_pid(self, tmp_path):
        from main import _acquire_daemon_lock

        pid_path = str(tmp_path / "daemon.pid")
        fd = _acquire_daemon_lock(pid_path)
        assert fd is not None

        # PID file contains our PID.
        with open(pid_path) as f:
            assert f.read().strip() == str(os.getpid())

        # flock is held — a second acquire must return None.
        fd2 = _acquire_daemon_lock(pid_path)
        assert fd2 is None

        fd.close()

    def test_lock_released_after_close(self, tmp_path):
        from main import _acquire_daemon_lock

        pid_path = str(tmp_path / "daemon.pid")
        fd = _acquire_daemon_lock(pid_path)
        assert fd is not None
        fd.close()

        # Now another acquire should succeed.
        fd2 = _acquire_daemon_lock(pid_path)
        assert fd2 is not None
        fd2.close()

    def test_creates_pid_file_if_missing(self, tmp_path):
        from main import _acquire_daemon_lock

        pid_path = str(tmp_path / "subdir" / "daemon.pid")
        # Parent doesn't exist — caller is responsible for that, but the
        # file itself gets created by open().
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        fd = _acquire_daemon_lock(pid_path)
        assert fd is not None
        assert os.path.isfile(pid_path)
        fd.close()

    def test_daemon_pid_path_uses_config(self, tmp_path):
        from main import _daemon_pid_path
        from tests.conftest import MockConfigManager

        cache_dir = str(tmp_path / "my_cache")
        config = MockConfigManager({"files": {"cache": {"dir": cache_dir}}})
        path = _daemon_pid_path(config)
        assert path == os.path.join(cache_dir, "daemon.pid")
        assert os.path.isdir(cache_dir)


# ---------------------------------------------------------------------------
# cli/stop.py helpers
# ---------------------------------------------------------------------------

class TestStopHelpers:
    def test_flock_is_held_true(self, tmp_path):
        from cli.stop import flock_is_held

        pid_path = str(tmp_path / "daemon.pid")
        fd = open(pid_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert flock_is_held(pid_path) is True
        fd.close()

    def test_flock_is_held_false_no_file(self, tmp_path):
        from cli.stop import flock_is_held

        assert flock_is_held(str(tmp_path / "nope.pid")) is False

    def test_flock_is_held_false_unlocked(self, tmp_path):
        from cli.stop import flock_is_held

        pid_path = str(tmp_path / "daemon.pid")
        open(pid_path, "w").close()  # create but don't lock
        assert flock_is_held(pid_path) is False

    def test_kill_by_pid_file_no_file(self, tmp_path):
        from cli.stop import kill_by_pid_file

        assert kill_by_pid_file(str(tmp_path / "nope.pid")) is False

    def test_kill_by_pid_file_stale_pid(self, tmp_path):
        from cli.stop import kill_by_pid_file

        pid_path = str(tmp_path / "daemon.pid")
        # Write a PID that almost certainly doesn't exist.
        with open(pid_path, "w") as f:
            f.write("999999999")
        assert kill_by_pid_file(pid_path) is False

    def test_kill_by_pid_file_sends_signal(self, tmp_path):
        """Spawn a sleep subprocess, write its PID, and verify SIGTERM kills it."""
        from cli.stop import kill_by_pid_file

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pid_path = str(tmp_path / "daemon.pid")
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))

        assert kill_by_pid_file(pid_path, signal.SIGTERM) is True
        proc.wait(timeout=5)
        assert proc.returncode != 0  # killed by signal

    def test_wait_for_flock_release(self, tmp_path):
        from cli.stop import wait_for_flock_release

        pid_path = str(tmp_path / "daemon.pid")
        open(pid_path, "w").close()
        # No lock held — should return immediately.
        assert wait_for_flock_release(pid_path, timeout=1.0) is True

    def test_wait_for_flock_release_timeout(self, tmp_path):
        from cli.stop import wait_for_flock_release

        pid_path = str(tmp_path / "daemon.pid")
        fd = open(pid_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Lock held — should time out.
        assert wait_for_flock_release(pid_path, timeout=0.5) is False
        fd.close()


# ---------------------------------------------------------------------------
# cli/stop.py stop_daemon integration
# ---------------------------------------------------------------------------

class TestStopDaemon:
    def test_stop_daemon_no_daemon_running(self, tmp_path, monkeypatch):
        """stop_daemon returns True when no daemon is running."""
        from cli.stop import stop_daemon
        from tests.conftest import MockConfigManager

        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)

        # Patch ConfigManager so stop_daemon looks in our tmp dir.
        monkeypatch.setattr(
            "cli.stop.ConfigManager",
            lambda: MockConfigManager({"files": {"cache": {"dir": cache_dir}}}),
        )
        assert stop_daemon() is True

    def test_stop_daemon_kills_subprocess(self, tmp_path, monkeypatch):
        """stop_daemon sends SIGTERM and waits for the flock to release."""
        from cli.stop import stop_daemon
        from tests.conftest import MockConfigManager

        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        pid_path = os.path.join(cache_dir, "daemon.pid")

        # Spawn a subprocess that holds the flock (mimics daemon).
        helper = (
            "import fcntl, time, sys\n"
            f"fd = open({pid_path!r}, 'w')\n"
            "fd.write(str(__import__('os').getpid()))\n"
            "fd.flush()\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", helper])
        # Wait for the flock to be acquired.
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.isfile(pid_path) and os.path.getsize(pid_path) > 0:
                break
            time.sleep(0.05)

        monkeypatch.setattr(
            "cli.stop.ConfigManager",
            lambda: MockConfigManager({"files": {"cache": {"dir": cache_dir}}}),
        )
        assert stop_daemon(timeout=5.0) is True
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# cli/daemon.py
# ---------------------------------------------------------------------------

class TestCliDaemon:
    def test_import_succeeds(self):
        """cli/daemon.py should import without error (no rabbitviewer_daemon dep)."""
        import importlib
        mod = importlib.import_module("cli.daemon")
        assert hasattr(mod, "main")
