"""
Persistent exiftool process using -stay_open mode.

One ExifToolProcess per worker thread eliminates per-file Perl startup overhead
(~200-500 ms on cold NAS paths). Numbered execute IDs make sentinel detection
safe for arbitrary binary output.
"""
import logging
import os
import select
import shutil
import subprocess
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# Registry of all live processes for orderly shutdown.
_all_processes: List["ExifToolProcess"] = []
_registry_lock = threading.Lock()

# Resolved absolute path to the exiftool binary (cached after first success).
_exiftool_path: Optional[str] = None

# Common install locations to check when exiftool is not on PATH
# (e.g. GUI apps launched from Dock inherit a minimal PATH).
_EXTRA_SEARCH_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
]


def _resolve_exiftool() -> Optional[str]:
    global _exiftool_path
    if _exiftool_path is not None:
        return _exiftool_path

    found = shutil.which("exiftool")
    if found:
        _exiftool_path = found
        return found

    for d in _EXTRA_SEARCH_DIRS:
        candidate = os.path.join(d, "exiftool")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):  # disk-io: binary discovery
            _exiftool_path = candidate
            return candidate

    return None


def is_exiftool_available() -> bool:
    """Return True if exiftool can be found.

    Positive results are cached permanently (exiftool won't vanish
    mid-session).  Negative results are retried each call so a late
    PATH fix or brew install takes effect without restarting.
    """
    return _resolve_exiftool() is not None


class ExifToolCancelled(Exception):
    """Raised when an exiftool command is cancelled via cancel_event."""


class ExifToolProcess:
    """Wraps a single persistent exiftool -stay_open process."""

    def __init__(self) -> None:
        self._process = self._spawn()
        self._counter = 0
        with _registry_lock:
            _all_processes.append(self)

    def _spawn(self) -> subprocess.Popen:
        path = _resolve_exiftool()
        if path is None:
            raise FileNotFoundError("exiftool not found on PATH or in common install directories")
        return subprocess.Popen(
            [path, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def execute(self, args: List[str],
                cancel_event: Optional[threading.Event] = None) -> bytes:
        """Send args to the persistent process and return its stdout bytes.

        Raises ``ExifToolCancelled`` if *cancel_event* is set during execution.
        The exiftool process is restarted after a cancel to flush stale output.
        """
        try:
            return self._do_execute(args, cancel_event=cancel_event)
        except ExifToolCancelled:
            raise  # why: don't retry a deliberate cancellation
        except Exception as e:
            logger.warning("ExifToolProcess: execute failed (%s); restarting.", e)
            self._restart()
            return self._do_execute(args, cancel_event=cancel_event)

    def _do_execute(self, args: List[str], timeout: float = 30.0,
                    cancel_event: Optional[threading.Event] = None) -> bytes:
        self._counter += 1
        exec_id = self._counter
        sentinel = f"{{ready{exec_id}}}\n".encode()

        cmd = "\n".join(args) + f"\n-execute{exec_id}\n"
        self._process.stdin.write(cmd.encode())  # type: ignore[union-attr]
        self._process.stdin.flush()              # type: ignore[union-attr]

        output = bytearray()
        sentinel_len = len(sentinel)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                # why: restart to flush the abandoned command's output;
                # the sentinel from this exec_id would corrupt the next call.
                self._restart()
                raise ExifToolCancelled(f"exiftool command cancelled (exec_id={exec_id})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"exiftool did not respond within {timeout}s")
            # why: cap select timeout at 0.5s so cancel_event is polled promptly
            poll_time = min(remaining, 0.5) if cancel_event is not None else remaining
            ready, _, _ = select.select([self._process.stdout], [], [], poll_time)
            if not ready:
                continue  # select timed out — re-check cancel_event and deadline
            chunk = self._process.stdout.read1(65536)  # type: ignore[union-attr]
            if not chunk:
                raise RuntimeError("exiftool process closed stdout unexpectedly")
            output.extend(chunk)
            if len(output) >= sentinel_len and output[-sentinel_len:] == sentinel:
                del output[-sentinel_len:]
                break

        return bytes(output)

    def _restart(self) -> None:
        try:
            self._process.kill()
            self._process.wait(timeout=2)
        except Exception:
            pass
        self._process = self._spawn()
        self._counter = 0
        logger.info("ExifToolProcess: restarted successfully.")

    def terminate(self) -> None:
        """Ask exiftool to exit cleanly, then force-kill if needed."""
        try:
            self._process.stdin.write(b"-stay_open\nFalse\n")  # type: ignore[union-attr]
            self._process.stdin.flush()                         # type: ignore[union-attr]
            self._process.wait(timeout=5)
        except Exception:
            pass
        finally:
            try:
                self._process.kill()
            except Exception:
                pass


def shutdown_all() -> None:
    """Terminate every registered ExifToolProcess. Called at daemon shutdown."""
    with _registry_lock:
        processes = list(_all_processes)
        _all_processes.clear()
    for proc in processes:
        proc.terminate()
    logger.info("ExifToolProcess: all %d process(es) terminated.", len(processes))
