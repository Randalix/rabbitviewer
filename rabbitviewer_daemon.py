import os
import sys
import logging
import signal
import threading
import time
import fcntl
import errno
from network.socket_thumbnailer import ThumbnailSocketServer
from core.thumbnail_manager import ThumbnailManager
from core.metadata_database import get_metadata_database
from core.background_indexer import BackgroundIndexer
from filewatcher.watcher import WatchdogHandler
from config.config_manager import ConfigManager


# Holds the exclusive lock fd; must not be GC'd for the process lifetime.
_instance_lock_fd = None


def _acquire_instance_lock(pid_file_path: str):
    parent = os.path.dirname(pid_file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = open(pid_file_path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            fd.seek(0)
            existing_pid = fd.read().strip()
            pid_info = f" (PID {existing_pid})" if existing_pid else ""
            print(
                f"RabbitViewer daemon is already running{pid_info}. Exiting.",
                file=sys.stderr,
            )
            fd.close()
            sys.exit(1)
        raise
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def setup_logging(log_level):
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    log_dir = os.path.expanduser("~/.rabbitviewer")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "daemon.log")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="a"),
            logging.StreamHandler(sys.stderr)
        ]
    )


def main():
    config_manager = ConfigManager()
    logging_level = config_manager.get("logging_level", "INFO")
    setup_logging(logging_level)
    logging.info(f"Logging level set to: {logging_level.upper()}")

    SOCKET_PATH = os.path.expanduser(config_manager.get("system.socket_path"))
    CACHE_DIR = os.path.expanduser(config_manager.get("files.cache.dir", "~/.rabbitviewer/cache"))
    WATCH_PATHS = [os.path.expanduser(path) for path in config_manager.get("watch_paths", [])]

    pid_file_path = os.path.join(CACHE_DIR, "daemon.pid")
    global _instance_lock_fd
    _instance_lock_fd = _acquire_instance_lock(pid_file_path)
    logging.info(f"Instance lock acquired: {pid_file_path}")

    logging.info(f"Configured watch paths: {WATCH_PATHS}")
    for path in WATCH_PATHS:
        if os.path.exists(path):
            logging.info(f"Watch path exists: {path}")
        else:
            logging.warning(f"Watch path does not exist: {path}")

    # why: crash leaves socket file bound; bind() raises EADDRINUSE without removal
    if os.path.exists(SOCKET_PATH):
        logging.warning(f"Removing existing socket file: {SOCKET_PATH}")
        os.remove(SOCKET_PATH)

    logging.info("Starting RabbitViewer Daemon...")

    metadata_db_path = os.path.join(CACHE_DIR, "metadata.db")
    logging.debug(f"Initializing MetadataDatabase with path: {metadata_db_path}")
    metadata_database = get_metadata_database(metadata_db_path)

    logging.debug("Initializing ThumbnailManager")
    thumbnail_manager = ThumbnailManager(config_manager, metadata_database)

    logging.debug(f"Initializing WatchdogHandler for paths: {WATCH_PATHS}")
    watcher = WatchdogHandler(thumbnail_manager, WATCH_PATHS, is_daemon_mode=True)

    def shutdown_service(signum=None, frame=None):
        logging.info("Shutting down RabbitViewer Daemon...")
        # Orderly shutdown: stop accepting new work, then stop workers.
        if server:
            logging.info("Shutting down socket server...")
            server.shutdown()
        if watcher:
            logging.info("Stopping file watcher...")
            watcher.stop()
        if thumbnail_manager:
            logging.info("Shutting down ThumbnailManager...")
            thumbnail_manager.shutdown()

        # MetadataDatabase uses SQLite WAL and doesn't need explicit shutdown.

        logging.info("Daemon shutdown complete.")
        global _instance_lock_fd
        if _instance_lock_fd is not None:
            try:
                _instance_lock_fd.close()
            except OSError:
                logging.warning("Failed to release instance lock fd on shutdown.")
            _instance_lock_fd = None
        sys.exit(0)

    server = None
    server_thread = None
    try:
        # 1. Construct the socket server — this binds the socket immediately,
        # creating the socket file and signalling to the GUI that the daemon is up.
        server = ThumbnailSocketServer(SOCKET_PATH, thumbnail_manager, watcher)
        logging.info("Socket bound. Loading plugins...")

        # 2. Load plugins now that the socket file exists so the GUI can start
        # connecting while we run the (potentially slow) is_available() checks.
        thumbnail_manager.load_plugins()
        server.directory_scanner._supported_extensions = set(thumbnail_manager.get_supported_formats())
        logging.info("Plugins loaded.")

        # 3. Start the accept loop.
        server_thread = threading.Thread(target=server.run_forever, daemon=True)
        server_thread.start()
        logging.info("Socket server thread started.")

        # 4. Start the file watcher for live filesystem events.
        logging.info("Starting file watcher...")
        watcher.start()

        # 5. Start continuous background indexing of watch_paths.
        background_indexer = BackgroundIndexer(
            thumbnail_manager, server.directory_scanner, WATCH_PATHS
        )
        background_indexer.start_indexing()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, shutdown_service)
        signal.signal(signal.SIGTERM, shutdown_service)

        # Keep the main thread alive
        while True:
            time.sleep(1)

    except Exception as e:  # why: startup failure must be logged before process dies; no narrower type covers all init failures
        logging.error(f"Daemon failed to start: {e}", exc_info=True)
        shutdown_service()


if __name__ == "__main__":
    main()
