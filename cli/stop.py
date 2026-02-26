"""Send a graceful shutdown signal to the RabbitViewer daemon."""

import errno
import fcntl
import logging
import os
import signal
import socket
import time

from config.config_manager import ConfigManager
from network.socket_client import ThumbnailSocketClient


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
    socket_path = config_manager.get("system.socket_path")

    if not socket_path:
        logging.error("system.socket_path not found in config.")
        return False

    pid_path = pid_file_path(config_manager)
    daemon_running = False

    logging.info(f"Sending stop signal to daemon at {socket_path}...")

    # Phase 1: try graceful shutdown via socket
    if os.path.exists(socket_path):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as temp_sock:
                temp_sock.settimeout(0.5)
                temp_sock.connect(socket_path)
            # Socket is accepting connections — attempt protocol-level shutdown
            daemon_running = True
            client = ThumbnailSocketClient(socket_path)
            try:
                if client.shutdown_daemon():
                    logging.info("Stop signal sent successfully.")
                else:
                    logging.warning("Graceful shutdown failed; will escalate via PID file.")
            finally:
                client.shutdown()
        except (socket.error, ConnectionRefusedError, socket.timeout):
            logging.info("Socket exists but daemon not accepting connections.")
            # May still be alive (hung); check PID file below
            daemon_running = flock_is_held(pid_path)
            if not daemon_running:
                logging.info(f"Stale socket file at {socket_path}; removing.")
                try:
                    os.remove(socket_path)
                except OSError as e:
                    logging.warning(f"Could not remove stale socket file: {e}")
                return True
    elif flock_is_held(pid_path):
        logging.info("Socket absent but daemon holds PID lock; killing by PID file.")
        daemon_running = True
        kill_by_pid_file(pid_path)
    else:
        logging.info("Daemon is not running (no socket, no PID lock).")
        return True

    if not daemon_running:
        return True

    # Phase 2: wait for flock release (proves daemon process exited)
    if wait_for_flock_release(pid_path, timeout=timeout):
        logging.info("Daemon exited cleanly.")
        # Clean up stale socket if daemon didn't remove it
        if os.path.exists(socket_path):
            try:
                os.remove(socket_path)
            except OSError:
                pass
        return True

    # Phase 3: escalate — SIGTERM via PID file
    logging.warning("Daemon did not exit in %.1fs; sending SIGTERM via PID file...", timeout)
    if kill_by_pid_file(pid_path, signal.SIGTERM):
        if wait_for_flock_release(pid_path, timeout=5.0):
            logging.info("Daemon exited after SIGTERM.")
            return True

    # Phase 4: SIGKILL
    logging.warning("Daemon still alive; sending SIGKILL...")
    kill_by_pid_file(pid_path, signal.SIGKILL)
    time.sleep(1.0)

    if not flock_is_held(pid_path):
        logging.info("Daemon killed.")
        if os.path.exists(socket_path):
            try:
                os.remove(socket_path)
            except OSError:
                pass
        return True

    logging.error("Failed to stop daemon even with SIGKILL.")
    return False


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    stop_daemon()


if __name__ == "__main__":
    main()
