"""Flock-based coordination between GUI and daemon processes.

The GUI acquires an exclusive flock on a lock file when it starts.
The daemon polls this lock to know when to pause/resume its workers.
"""
import fcntl
import errno
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_DIR = os.path.expanduser("~/.rabbitviewer")
_GUI_LOCK_FILENAME = "gui.lock"


def _lock_path(lock_dir: str = _DEFAULT_LOCK_DIR) -> str:
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, _GUI_LOCK_FILENAME)


def acquire_gui_lock(lock_dir: str = _DEFAULT_LOCK_DIR):
    """Acquire an exclusive flock for the GUI process.

    Returns the open file descriptor (must be kept alive for the lock
    to remain held). Returns None if another GUI already holds the lock.
    """
    path = _lock_path(lock_dir)
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        logger.info("GUI lock acquired: %s", path)
        return fd
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            logger.info("Another GUI already holds the lock.")
            fd.close()
            return None
        raise


def release_gui_lock(fd) -> None:
    """Release the GUI flock."""
    if fd is not None:
        try:
            fd.close()
        except OSError:
            pass
        logger.info("GUI lock released.")


def is_gui_active(lock_dir: str = _DEFAULT_LOCK_DIR) -> bool:
    """Check whether a GUI process currently holds the lock (non-blocking)."""
    path = _lock_path(lock_dir)
    if not os.path.exists(path):
        return False
    try:
        fd = open(path, "a+")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — no GUI is active. Release immediately.
        fd.close()
        return False
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        raise
