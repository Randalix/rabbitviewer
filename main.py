import logging
import sys
import os
import argparse
import signal
import subprocess
import time

from config.config_manager import ConfigManager
from core.metadata_database import get_metadata_database
from core.thumbnail_manager import ThumbnailManager
from core.directory_scanner import DirectoryScanner
from core.cache_size_manager import CacheSizeManager
from core.resource_lock import acquire_gui_lock, release_gui_lock, is_gui_active
from filewatcher.watcher import WatchdogHandler


def setup_logging(log_level, log_filename="rabbitviewer.log"):
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    log_dir = os.path.expanduser("~/.rabbitviewer")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_filename)

    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_path, maxBytes=50 * 1024 * 1024, backupCount=3,
    )

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[file_handler, logging.StreamHandler(sys.stdout)],
    )
    # PIL dumps every TIFF/EXIF tag at DEBUG, including raw binary values.
    logging.getLogger("PIL").setLevel(logging.INFO)


def _init_core(config_manager):
    """Initialize ThumbnailManager, WatchdogHandler, and supporting objects."""
    cache_dir = os.path.expanduser(config_manager.get("files.cache.dir", "~/.rabbitviewer/cache"))
    metadata_db_path = os.path.join(cache_dir, "metadata.db")
    metadata_db = get_metadata_database(metadata_db_path)

    thumbnail_manager = ThumbnailManager(config_manager, metadata_db)

    max_cache_mb = config_manager.get("max_cache_size_mb", 0)
    cache_size_manager = CacheSizeManager(metadata_db, max_cache_mb)
    thumbnail_manager.cache_size_manager = cache_size_manager
    thumbnail_manager.render_manager.cache_size_manager = cache_size_manager

    thumbnail_manager.load_plugins()
    logging.info("Plugins loaded.")

    watch_paths = [os.path.expanduser(p) for p in config_manager.get("watch_paths", [])]
    watcher = WatchdogHandler(thumbnail_manager, watch_paths)
    thumbnail_manager.watchdog_handler = watcher

    return thumbnail_manager, watcher, watch_paths


# ---------------------------------------------------------------------------
# Daemon mode  (main.py --daemon)
# ---------------------------------------------------------------------------

def _run_daemon(config_manager):
    """Headless background indexer.  Pauses when the GUI holds the flock."""
    from core.background_indexer import BackgroundIndexer

    setup_logging(config_manager.get("logging_level", "INFO"), log_filename="daemon.log")
    logging.info("Starting RabbitViewer daemon (headless indexer)")

    thumbnail_manager, watcher, watch_paths = _init_core(config_manager)
    directory_scanner = DirectoryScanner(thumbnail_manager, config_manager)

    watcher.start()

    background_indexer = BackgroundIndexer(thumbnail_manager, directory_scanner, watch_paths)
    background_indexer.start_indexing()

    rm = thumbnail_manager.render_manager
    gui_was_active = False

    def _shutdown(signum=None, frame=None):
        logging.info("Daemon shutting down...")
        watcher.stop()
        thumbnail_manager.shutdown()
        logging.info("Daemon shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Poll the GUI flock every 2 seconds.
    while True:
        try:
            gui_active = is_gui_active()
            if gui_active and not gui_was_active:
                logging.info("GUI detected — pausing daemon workers.")
                rm.pause()
                gui_was_active = True
            elif not gui_active and gui_was_active:
                logging.info("GUI gone — resuming daemon workers.")
                rm.resume()
                gui_was_active = False
        except Exception as e:
            logging.debug(f"GUI lock poll error: {e}")
        time.sleep(2)


def _auto_launch_daemon():
    """Spawn the daemon as a detached subprocess if not already running."""
    if is_gui_active():
        # Another GUI is running — it already has workers. Don't spawn another daemon.
        return
    # The daemon uses its own instance lock, so a double-launch is harmless (second exits).
    script = os.path.abspath(__file__)
    subprocess.Popen(
        [sys.executable, script, "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logging.info("Auto-launched daemon subprocess.")


# ---------------------------------------------------------------------------
# GUI mode  (default)
# ---------------------------------------------------------------------------

def _run_gui(args, config_manager):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import QTimer
    from core.thumbnail_service import ThumbnailService
    from network.daemon_signals import DaemonSignals
    from gui.main_window import MainWindow

    target_dir = args.directory
    recursive_scan = args.recursive

    setup_logging(config_manager.get("logging_level", "INFO"))
    logging.info("Starting RabbitViewer")

    if args.cold_cache and target_dir:
        from benchmarks.bench_utils import cold_cache
        cold_dir = os.path.abspath(target_dir)
        logging.info("--cold-cache: deleting cached metadata for %s", cold_dir)
        rows, files = cold_cache(cold_dir)
        logging.info("--cold-cache: %d DB rows deleted, %d cache files removed", rows, files)

    # Acquire the GUI flock — tells daemon to yield resources.
    gui_lock_fd = acquire_gui_lock()
    if gui_lock_fd is None:
        print("Another RabbitViewer GUI is already running.", file=sys.stderr)
        return 1

    thumbnail_manager, watcher, watch_paths = _init_core(config_manager)
    directory_scanner = DirectoryScanner(thumbnail_manager, config_manager)
    service = ThumbnailService(thumbnail_manager, directory_scanner)

    watcher.start()

    # Auto-launch daemon for background indexing when GUI exits.
    _auto_launch_daemon()

    # --- Qt Application ---
    app = QApplication(sys.argv)
    app.setApplicationName("Rabbit Viewer")

    daemon_signals = DaemonSignals()
    thumbnail_manager.render_manager.add_notification_callback(
        daemon_signals.dispatch_notification
    )

    icon_path = os.path.join(os.path.dirname(__file__), "logo", "rabbitViewerLogo.png")
    app.setWindowIcon(QIcon(icon_path))

    if target_dir:
        target_dir = os.path.abspath(target_dir)
        if not os.path.isdir(target_dir):
            logging.error(f"Invalid directory provided: {target_dir}")
            release_gui_lock(gui_lock_fd)
            return 1

    window = MainWindow(config_manager, service, daemon_signals)

    def _shutdown():
        watcher.stop()
        thumbnail_manager.shutdown()
        release_gui_lock(gui_lock_fd)

    app.aboutToQuit.connect(_shutdown)

    window.show()
    app.processEvents()
    logging.info("[startup] window shown, services ready")

    if target_dir:
        QTimer.singleShot(0, lambda: window.load_directory(target_dir, recursive_scan))

    exit_code = app.exec()

    logging.info(f"Application exiting with code {exit_code}.")
    return exit_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RabbitViewer: A fast image viewer.")
    parser.add_argument('directory', nargs='?', default=None, help='The directory to open.')
    parser.add_argument(
        '--recursive',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Scan the directory recursively. Use --no-recursive to disable.',
    )
    parser.add_argument(
        '--cold-cache',
        action='store_true',
        default=False,
        help='Delete cached metadata for the target directory so everything '
             'is re-extracted from scratch.',
    )
    parser.add_argument(
        '--daemon',
        action='store_true',
        default=False,
        help='Run as headless background indexer (no GUI).',
    )
    args = parser.parse_args()

    config_manager = ConfigManager()

    if args.daemon:
        _run_daemon(config_manager)
    else:
        return _run_gui(args, config_manager)


if __name__ == "__main__":
    sys.exit(main() or 0)
