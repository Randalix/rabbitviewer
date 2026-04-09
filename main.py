import logging
import sys
import os
import argparse
import signal
import subprocess
import threading
import time

# why: __name__ is "__main__" at runtime; "rabbitviewer" is a stable name
# reachable from logging_levels config overrides.
logger = logging.getLogger("rabbitviewer")

from config.config_manager import ConfigManager
from core.metadata_database import get_metadata_database
from core.thumbnail_manager import ThumbnailManager
from core.directory_scanner import DirectoryScanner
from core.cache_size_manager import CacheSizeManager
from core.resource_lock import acquire_gui_lock, release_gui_lock, is_gui_active
from filewatcher.watcher import WatchdogHandler


def setup_logging(log_level, log_filename="rabbitviewer.log", logging_levels=None):
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

    # Per-module log level overrides from config.
    if logging_levels:
        for module_name, level_str in logging_levels.items():
            numeric = getattr(logging, level_str.upper(), None)
            if numeric is not None:
                logging.getLogger(module_name).setLevel(numeric)


def _init_core(config_manager):
    """Initialize ThumbnailManager, WatchdogHandler, and supporting objects."""
    cache_dir = os.path.expanduser(config_manager.get("files.cache.dir", "~/.rabbitviewer/cache"))
    metadata_db_path = os.path.join(cache_dir, "metadata.db")
    metadata_db = get_metadata_database(metadata_db_path)

    thumbnail_manager = ThumbnailManager(config_manager, metadata_db)

    max_cache_mb = config_manager.get("max_cache_size_mb", 0)
    max_disk_pct = config_manager.get("max_disk_usage_pct", 90.0)
    cache_size_manager = CacheSizeManager(metadata_db, max_cache_mb,
                                          cache_dir=cache_dir,
                                          max_disk_usage_pct=max_disk_pct)
    thumbnail_manager.cache_size_manager = cache_size_manager
    thumbnail_manager.render_manager.cache_size_manager = cache_size_manager

    thumbnail_manager.load_plugins()
    logger.info("Plugins loaded.")

    watch_paths = [os.path.expanduser(p) for p in config_manager.get("watch_paths", [])]
    watcher = WatchdogHandler(thumbnail_manager, watch_paths)
    thumbnail_manager.watchdog_handler = watcher

    return thumbnail_manager, watcher, watch_paths


# ---------------------------------------------------------------------------
# Daemon mode  (main.py --daemon)
# ---------------------------------------------------------------------------

def _daemon_pid_path(config_manager):
    """Return the path to the daemon PID file."""
    cache_dir = os.path.expanduser(
        config_manager.get("files.cache.dir", "~/.rabbitviewer/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "daemon.pid")


def _acquire_daemon_lock(pid_path):
    """Write PID and hold an flock so cli/stop.py can find us.

    Returns the open fd (must stay alive), or None if another daemon
    already holds the lock.
    """
    import fcntl, errno
    fd = open(pid_path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            fd.close()
            return None
        raise


def _read_git_head():
    """Return the current git HEAD commit hash, or None on failure."""
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        head_path = os.path.join(repo_dir, ".git", "HEAD")
        with open(head_path) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            # Try loose ref first, fall back to packed-refs.
            ref_path = os.path.join(repo_dir, ".git", ref)
            try:
                with open(ref_path) as f:
                    return f.read().strip()
            except OSError:
                pass
            # Packed refs: lines like "<hash> <ref>"
            packed_path = os.path.join(repo_dir, ".git", "packed-refs")
            try:
                with open(packed_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("#") or not line:
                            continue
                        parts = line.split(" ", 1)
                        if len(parts) == 2 and parts[1] == ref:
                            return parts[0]
            except OSError:
                pass
            return None
        return head  # detached HEAD
    except OSError:
        return None


def _run_daemon(config_manager, log_level_override=None):
    """Headless background indexer.  Pauses when the GUI holds the flock."""
    from core.background_indexer import BackgroundIndexer

    log_level = log_level_override or config_manager.get("logging_level", "INFO")
    setup_logging(log_level, log_filename="daemon.log",
                  logging_levels=config_manager.get("logging_levels", {}))

    pid_path = _daemon_pid_path(config_manager)
    pid_fd = _acquire_daemon_lock(pid_path)
    if pid_fd is None:
        logger.info("Another daemon is already running; exiting.")
        return

    logger.info("Starting RabbitViewer daemon (headless indexer)")

    thumbnail_manager, watcher, watch_paths = _init_core(config_manager)
    directory_scanner = DirectoryScanner(thumbnail_manager, config_manager,
                                        source_cache=thumbnail_manager.source_cache)

    watcher.start()

    background_indexer = BackgroundIndexer(
        thumbnail_manager, directory_scanner, watch_paths,
        metadata_db=thumbnail_manager.metadata_db,
    )
    background_indexer.start_indexing()

    # Warm AI models in background before indexing tasks start consuming workers.
    # Daemon prewarm runs at QOS_CLASS_BACKGROUND; no UI to freeze.
    thumbnail_manager.prewarm_models()

    rm = thumbnail_manager.render_manager
    gui_was_active = False
    startup_head = _read_git_head()

    def _shutdown(signum=None, frame=None):
        logger.info("Daemon shutting down...")
        watcher.stop()
        thumbnail_manager.shutdown()
        pid_fd.close()
        logger.info("Daemon shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Poll the GUI flock every 2 seconds.
    while True:
        try:
            gui_active = is_gui_active()
            if gui_active and not gui_was_active:
                logger.info("GUI detected — pausing daemon workers.")
                rm.pause()
                gui_was_active = True
            elif not gui_active and gui_was_active:
                logger.info("GUI gone — resuming daemon workers.")
                rm.resume()
                background_indexer.recover_pending_transfers()
                background_indexer.recover_orphans()
                background_indexer.resume_pending_work()
                thumbnail_manager.recover_pending_writes()
                gui_was_active = False
        except Exception as e:  # why: is_gui_active() can raise OSError on remote fs; must not crash poll loop
            logger.debug("GUI lock poll error: %s", e)

        # Auto-restart when code changes (git HEAD moves).
        if startup_head is not None:
            current_head = _read_git_head()
            if current_head is not None and current_head != startup_head:
                logger.info("Code changed (HEAD %s → %s) — restarting daemon.",
                            startup_head[:8], current_head[:8])
                watcher.stop()
                thumbnail_manager.shutdown()
                pid_fd.close()
                os.execv(sys.executable, [sys.executable] + sys.argv)

        time.sleep(2)


def _launch_daemon():
    """Spawn the daemon as a detached subprocess.

    The daemon acquires its own PID lock, so double-launches are harmless
    (the second process exits immediately).
    """
    script = os.path.abspath(__file__)
    subprocess.Popen(
        [sys.executable, script, "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("Launched daemon subprocess.")


# ---------------------------------------------------------------------------
# GUI mode  (default)
# ---------------------------------------------------------------------------

def _set_macos_app_name(name):
    """Set the macOS menu bar name via the main bundle's Info.plist."""
    import ctypes
    import ctypes.util

    lib = ctypes.util.find_library('objc')
    if not lib:
        return
    objc = ctypes.cdll.LoadLibrary(lib)
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_msgSend.restype = ctypes.c_void_p
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    NSBundle = objc.objc_getClass(b'NSBundle')
    bundle = objc.objc_msgSend(NSBundle, objc.sel_registerName(b'mainBundle'))
    info = objc.objc_msgSend(bundle, objc.sel_registerName(b'infoDictionary'))

    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
    NSString = objc.objc_getClass(b'NSString')
    sel_str = objc.sel_registerName(b'stringWithUTF8String:')
    key = objc.objc_msgSend(NSString, sel_str, b'CFBundleName')
    value = objc.objc_msgSend(NSString, sel_str, name.encode('utf-8'))

    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    objc.objc_msgSend(info, objc.sel_registerName(b'setObject:forKey:'), value, key)


def _run_gui(args, config_manager):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import QTimer, QEvent, qInstallMessageHandler, QtMsgType
    from network.daemon_signals import DaemonSignals
    from gui.main_window import MainWindow

    target_dir = args.directory
    target_file = None
    recursive_scan = args.recursive

    log_level = args.log_level or config_manager.get("logging_level", "INFO")
    setup_logging(log_level,
                  logging_levels=config_manager.get("logging_levels", {}))
    logger.info("Starting RabbitViewer")

    # Add a signal handler for SIGUSR1 to dump all thread stacks for debugging freezes.
    def _dump_threads(signum, frame):
        import traceback
        logger.info(f"Received signal {signum}. Dumping all thread stacks:")
        for thread_id, stack in sys._current_frames().items():
            logger.info(f"\n--- Thread {thread_id} ---")
            for filename, lineno, name, line in traceback.extract_stack(stack):
                logger.info(f'  File "{filename}", line {lineno}, in {name}')
                if line:
                    logger.info(f'    {line.strip()}')
    
    # On Windows, SIGUSR1 is not available. This will raise an AttributeError.
    try:
        signal.signal(signal.SIGUSR1, _dump_threads)
    except AttributeError:
        logger.warning("SIGUSR1 not available on this platform; thread dump signal handler disabled.")

    # why: PySide6 swallows unhandled slot exceptions without logging; this surfaces them.
    def _handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = _handle_exception

    # If the user (or macOS) passed a file instead of a directory,
    # open the parent directory and navigate to that file.
    if target_dir:
        target_dir = os.path.abspath(target_dir)
        if os.path.isfile(target_dir):
            target_file = target_dir
            target_dir = os.path.dirname(target_file)
            recursive_scan = False

    if args.cold_cache and target_dir:
        from benchmarks.bench_utils import cold_cache
        cold_dir = os.path.abspath(target_dir)
        logger.info("--cold-cache: deleting cached metadata for %s", cold_dir)
        rows, files = cold_cache(cold_dir)
        logger.info("--cold-cache: %d DB rows deleted, %d cache files removed", rows, files)

    # Acquire the GUI flock — tells daemon to yield resources.
    gui_lock_fd = acquire_gui_lock()
    if gui_lock_fd is None:
        print("Another RabbitViewer GUI is already running.", file=sys.stderr)
        return 1

    if target_dir and not os.path.isdir(target_dir):
        logger.error(f"Invalid directory provided: {target_dir}")
        release_gui_lock(gui_lock_fd)
        return 1

    # --- Qt Application ---
    # Build the Qt app and show the window immediately so the user sees the UI
    # while core services (DB, plugins, workers) initialize in the background.
    if sys.platform == 'darwin':
        _set_macos_app_name("Rabbit Viewer")
    app = QApplication(sys.argv)
    app.setApplicationName("Rabbit Viewer")
    app.setProperty("monospace_font",
                     config_manager.get("gui.monospace_font", "monospace"))

    # why: Qt C++ warnings are otherwise lost; route them to rabbitviewer.log.
    _qt_logger = logging.getLogger("qt")
    _qt_level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _qt_message_handler(mode, context, message):
        level = _qt_level_map.get(mode, logging.WARNING)
        _qt_logger.log(level, message)

    qInstallMessageHandler(_qt_message_handler)

    daemon_signals = DaemonSignals()

    icon_path = os.path.join(os.path.dirname(__file__), "logo", "rabbitViewerLogo.png")
    app.setWindowIcon(QIcon(icon_path))

    # Show window with service=None; service is injected via _service_ready once ready.
    window = MainWindow(config_manager, None, daemon_signals,
                        debug_ui=args.debug_ui)

    # macOS "Open With" sends files via QFileOpenEvent (Apple Events),
    # not as CLI arguments.  Install a handler so the window receives them.
    def _file_open_handler(obj, event):
        if event.type() == QEvent.Type.FileOpen:
            path = event.file()
            if path and os.path.isfile(path):
                svc = window.service
                if not svc:
                    logger.debug("QFileOpenEvent: service not ready, queuing %s", path)
                    window._pending_file_open = path
                    return True
                _, ext = os.path.splitext(path)
                if not ext or ext.lower() not in svc.tm.supported_formats:
                    logger.debug("QFileOpenEvent: ignoring unsupported file %s", path)
                    return False
                logger.info("QFileOpenEvent: %s", path)
                parent = os.path.dirname(path)
                window.load_directory(parent, recursive=False)
                window.open_media_view(path)
                return True
        return False

    app.eventFilter = _file_open_handler
    app.installEventFilter(app)

    window.show()
    app.processEvents()
    logger.info("[startup] window shown, initializing core services in background")

    # _core_state lets shutdown find managers even if the scheduler is still
    # running when the event loop exits (user closes window during init).
    _core_state = {"thumbnail_manager": None, "watcher": None}

    # Qt-free bridge: emits _startup_task_done on the main thread when any
    # scheduler task completes.  QueuedConnection auto-applies across threads.
    from PySide6.QtCore import QObject, Signal as _Signal

    class _Bridge(QObject):
        task_done = _Signal(str, object)

    _bridge = _Bridge()
    _bridge.task_done.connect(window._startup_task_done)

    _init_cancelled = threading.Event()
    app.aboutToQuit.connect(_init_cancelled.set)

    # Only tasks with GUI handlers need to cross the thread boundary.
    _GUI_TASKS = {"service", "recover"}

    def _on_task_done(name: str, result) -> None:
        if name in _GUI_TASKS and not _init_cancelled.is_set():
            _bridge.task_done.emit(name, result)

    from core.startup_scheduler import StartupScheduler, Phase
    from core.thumbnail_service import ThumbnailService
    from core.thumbnail_manager import preload_plugins

    sched = StartupScheduler(on_task_done=_on_task_done)

    # ── Phase 10: independent tasks run in parallel ──────────────────────────

    _files_cache_dir = os.path.expanduser(
        config_manager.get("files.cache.dir", "~/.rabbitviewer/cache")
    )

    def _task_db(r):
        metadata_db_path = os.path.join(_files_cache_dir, "metadata.db")
        return get_metadata_database(metadata_db_path)

    def _task_plugins(r):
        # why: populates the global plugin_registry as a side effect; returns None.
        # _task_core calls thumbnail_manager.load_plugins() which skips the directory
        # scan when the registry is already populated, making that call near-instant.
        preload_plugins(config_manager)

    sched.add("db",      Phase.PARALLEL_INIT, _task_db)
    sched.add("plugins", Phase.PARALLEL_INIT, _task_plugins)

    # ── Phase 20: ThumbnailManager (needs DB; plugins already in registry) ───

    def _task_core(r):
        metadata_db = r["db"]
        thumbnail_manager = ThumbnailManager(config_manager, metadata_db)

        max_cache_mb = config_manager.get("max_cache_size_mb", 0)
        max_disk_pct = config_manager.get("max_disk_usage_pct", 90.0)
        cache_size_manager = CacheSizeManager(
            metadata_db, max_cache_mb,
            cache_dir=_files_cache_dir, max_disk_usage_pct=max_disk_pct,
        )
        thumbnail_manager.cache_size_manager = cache_size_manager
        thumbnail_manager.render_manager.cache_size_manager = cache_size_manager

        thumbnail_manager.load_plugins()  # fast: registry already populated
        _core_state["thumbnail_manager"] = thumbnail_manager
        return thumbnail_manager

    sched.add("core", Phase.CORE, _task_core)

    # ── Phase 30: service wiring (needs core) ────────────────────────────────

    watch_paths = [
        os.path.expanduser(p) for p in config_manager.get("watch_paths", [])
    ]

    def _task_service(r):
        tm = r["core"]
        watcher = WatchdogHandler(tm, watch_paths)
        tm.watchdog_handler = watcher
        _core_state["watcher"] = watcher

        directory_scanner = DirectoryScanner(
            tm, config_manager, source_cache=tm.source_cache
        )
        service = ThumbnailService(tm, directory_scanner)
        tm.render_manager.add_notification_callback(
            daemon_signals.dispatch_notification
        )
        logger.info("[startup] core services ready")
        return {
            "service": service,
            "watcher": watcher,
            "target_dir": target_dir,
            "target_file": target_file,
            "recursive": recursive_scan,
        }

    sched.add("service", Phase.SERVICE, _task_service)

    # ── Phase 50: supporting tasks (run in parallel, need service) ───────────

    def _task_watcher_start(r):
        # NAS directory probing can take 9-16 s — run off the GUI thread.
        threading.Thread(target=r["service"]["watcher"].start, daemon=True).start()

    def _task_recover(r):
        return r["service"]["service"].recover_pending_writes()

    sched.add("watcher", Phase.SUPPORTING, _task_watcher_start)
    sched.add("recover", Phase.SUPPORTING, _task_recover)

    # ── Start the scheduler after first frame paints ─────────────────────────
    QTimer.singleShot(0, sched.run)

    exit_code = app.exec()

    # Blocking shutdown runs after the event loop exits so the window
    # can close and repaint immediately rather than freezing.
    logger.info("GUI closed. Shutting down workers...")
    watcher = _core_state.get("watcher")
    thumbnail_manager = _core_state.get("thumbnail_manager")
    if watcher:
        watcher.stop()
    if thumbnail_manager:
        thumbnail_manager.shutdown()
    release_gui_lock(gui_lock_fd)

    # Launch daemon after releasing flock so it can start indexing immediately.
    _launch_daemon()

    logger.info(f"Application exiting with code {exit_code}.")
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
        default=False,
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
    parser.add_argument(
        '--stop-daemon',
        action='store_true',
        default=False,
        help='Stop the running daemon and exit.',
    )
    parser.add_argument(
        '--restart-daemon',
        action='store_true',
        default=False,
        help='Stop the running daemon, then start a new one.',
    )
    parser.add_argument(
        '--log-level',
        default=None,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Override logging level from config.',
    )
    parser.add_argument(
        '--debug-ui',
        action='store_true',
        default=False,
        help='Draw red borders on all widgets for layout debugging.',
    )
    args = parser.parse_args()

    config_manager = ConfigManager()

    if args.stop_daemon:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
        from cli.stop import stop_daemon
        sys.exit(0 if stop_daemon() else 1)

    if args.restart_daemon:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
        from cli.stop import stop_daemon
        if not stop_daemon():
            logger.error("Could not stop existing daemon; aborting restart.")
            sys.exit(1)
        logger.info("Restarting daemon...")
        args.daemon = True

    if args.daemon:
        _run_daemon(config_manager, log_level_override=args.log_level)
    else:
        return _run_gui(args, config_manager)


if __name__ == "__main__":
    sys.exit(main() or 0)
