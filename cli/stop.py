"""Send a graceful shutdown signal to the RabbitViewer daemon."""

import errno
import fcntl
import logging
import os
import signal
import time

from config.config_manager import ConfigManager


def pid_file_path(config_manager: ConfigManager | None = None) -> str:
    if config_manager is None:
        config_manager = ConfigManager()
    cache_dir = os.path.expanduser(
        config_manager.get("files.cache.dir", "~/.rabbitviewer/cache")
    )
    return os.path.join(cache_dir, "daemon.pid")


def flock_is_held(pid_path: str) -> bool:
    try:
        with open(pid_path, "r") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        raise


def kill_by_pid_file(pid_path: str, sig: int = signal.SIGTERM) -> bool:
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        logging.error("No permission to signal daemon PID %d", pid)
        return False


def wait_for_flock_release(pid_path: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while flock_is_held(pid_path):
        if time.time() > deadline:
            return False
        time.sleep(0.2)
    return True


def stop_daemon(timeout: float = 5.0) -> bool:
    """Stop the running daemon. Returns True if it shut down cleanly."""
    config_manager = ConfigManager()
    pid_path = pid_file_path(config_manager)

    if not flock_is_held(pid_path):
        logging.info("Daemon is not running (no PID lock).")
        return True

    logging.info("Daemon holds PID lock; sending SIGTERM...")
    kill_by_pid_file(pid_path, signal.SIGTERM)

    if wait_for_flock_release(pid_path, timeout=timeout):
        logging.info("Daemon exited cleanly.")
        return True

    # Escalate: SIGKILL
    logging.warning("Daemon did not exit in %.1fs; sending SIGKILL...", timeout)
    kill_by_pid_file(pid_path, signal.SIGKILL)
    time.sleep(1.0)

    if not flock_is_held(pid_path):
        logging.info("Daemon killed.")
        return True

    logging.error("Failed to stop daemon even with SIGKILL.")
    return False


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    stop_daemon()


if __name__ == "__main__":
    main()
