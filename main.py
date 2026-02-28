import logging
import sys
import os
import argparse
import subprocess
import tempfile
import signal
import time
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer
from config.config_manager import ConfigManager
from gui.main_window import MainWindow
from network.socket_client import ThumbnailSocketClient
from network.notification_client import NotificationListener
from network.daemon_signals import DaemonSignals
from cli.stop import pid_file_path as _pid_file_path, flock_is_held as _flock_is_held, \
    kill_by_pid_file as _kill_by_pid_file, wait_for_flock_release as _wait_for_flock_release

def setup_logging(log_level):
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    log_dir = os.path.expanduser("~/.rabbitviewer")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "rabbitviewer.log")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="a"),
            logging.StreamHandler(sys.stdout)
        ]
    )


def main():
    parser = argparse.ArgumentParser(description="RabbitViewer: A fast image viewer.")
    parser.add_argument('directory', nargs='?', default=None, help='The directory to open.')
    parser.add_argument(
        '--recursive',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Scan the directory recursively. Use --no-recursive to disable.'
    )
    parser.add_argument(
        '--restart-daemon',
        action='store_true',
        default=False,
        help='Shut down any running daemon and start a fresh one before launching.'
    )
    parser.add_argument(
        '--cold-cache',
        action='store_true',
        default=False,
        help='Delete cached metadata for the target directory so everything '
             'is re-extracted from scratch. Implies --restart-daemon.'
    )
    args = parser.parse_args()
    if args.cold_cache:
        args.restart_daemon = True
    target_dir = args.directory
    recursive_scan = args.recursive

    config_manager = ConfigManager()

    logging_level = config_manager.get("logging_level", "INFO")
    setup_logging(logging_level)

    logging.info("Starting RabbitViewer GUI")

    socket_path = os.path.expanduser(config_manager.get("system.socket_path", "/tmp/rabbitviewer_thumbnailer.sock"))
    socket_client = ThumbnailSocketClient(socket_path)
    pid_path = _pid_file_path(config_manager)

    if args.restart_daemon:
        daemon_was_running = False
        if socket_client.is_socket_file_present():
            logging.info("--restart-daemon: sending shutdown via socket...")
            socket_client.shutdown_daemon()
            daemon_was_running = True
        elif _flock_is_held(pid_path):
            logging.info("--restart-daemon: socket absent, killing daemon by PID file...")
            _kill_by_pid_file(pid_path)
            daemon_was_running = True

        if daemon_was_running and not _wait_for_flock_release(pid_path):
            logging.warning("Daemon did not release flock in 15 s; escalating to SIGKILL...")
            _kill_by_pid_file(pid_path, signal.SIGKILL)
            time.sleep(1.0)
        logging.info("--restart-daemon: daemon stopped.")

    if args.cold_cache and target_dir:
        from benchmarks.bench_utils import cold_cache
        cold_dir = os.path.abspath(target_dir)
        logging.info("--cold-cache: deleting cached metadata for %s", cold_dir)
        rows, files = cold_cache(cold_dir)
        logging.info("--cold-cache: %d DB rows deleted, %d cache files removed", rows, files)

    _daemon_log_path = None
    if not _flock_is_held(pid_path):
        logging.info("Daemon not running, launching it...")
        daemon_script = os.path.join(os.path.dirname(__file__), "rabbitviewer_daemon.py")
        log_file = tempfile.NamedTemporaryFile(delete=False, suffix='.log')
        _daemon_log_path = log_file.name
        subprocess.Popen(
            [sys.executable, daemon_script],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
        log_file.close()

    app = QApplication(sys.argv)
    app.setApplicationName("Rabbit Viewer")

    # DaemonSignals must be created after QApplication.
    daemon_signals = DaemonSignals()

    # Start notification listener immediately — it retries with backoff
    # until the daemon socket appears, so daemon lateness is fine.
    notification_listener = NotificationListener(socket_path, daemon_signals)
    notification_listener.start()
    logging.info("Notification listener thread started.")
    icon_path = os.path.join(os.path.dirname(__file__), "logo", "rabbitViewerLogo.png")
    app.setWindowIcon(QIcon(icon_path))

    if target_dir:
        target_dir = os.path.abspath(target_dir)
        if not os.path.isdir(target_dir):
            logging.error(f"Invalid directory provided: {target_dir}")
            return 1

    window = MainWindow(config_manager, socket_client, daemon_signals)

    app.aboutToQuit.connect(socket_client.shutdown)
    app.aboutToQuit.connect(notification_listener.stop)

    window.show()
    app.processEvents()
    logging.info("[startup] window shown")

    # Poll for daemon socket non-blockingly so the window stays responsive.
    # Once the socket appears, fire load_directory; on timeout, log an error.
    _poll_deadline = time.time() + 10.0
    _poll_target_dir = target_dir
    _poll_recursive = recursive_scan

    def _poll_for_daemon():
        if socket_client.is_socket_file_present():
            _daemon_poll.stop()
            logging.info("[startup] daemon socket ready")
            if _poll_target_dir:
                window.load_directory(_poll_target_dir, _poll_recursive)
            return
        if time.time() > _poll_deadline:
            _daemon_poll.stop()
            msg = "Daemon not available after 10 seconds."
            if _daemon_log_path:
                msg += f" See daemon log: {_daemon_log_path}"
            logging.error(msg)

    # why: parent=window prevents GC from collecting the timer before it fires
    _daemon_poll = QTimer(window)
    _daemon_poll.setInterval(200)
    _daemon_poll.timeout.connect(_poll_for_daemon)
    # If daemon is already running, skip the timer entirely.
    if socket_client.is_socket_file_present():
        logging.info("[startup] daemon socket ready")
        if target_dir:
            QTimer.singleShot(0, lambda: window.load_directory(target_dir, recursive_scan))
    else:
        _daemon_poll.start()

    exit_code = app.exec()

    logging.info(f"Application exiting with code {exit_code}.")
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
