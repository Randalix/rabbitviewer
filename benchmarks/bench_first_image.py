"""
Time-till-first-image benchmark — end-to-end, full stack.

Measures wall-clock time from daemon process spawn (t=0) until each
`previews_ready` notification arrives — the exact moment the GUI would
draw that thumbnail on screen.

Steps performed:
  1. Kill any running daemon
  2. Purge cached thumbnails for the target directory (DB + disk)
     so the daemon must regenerate everything from scratch
  3. Start a fresh daemon subprocess
  4. Register as a notification listener (mirrors NotificationListener)
  5. Send get_directory_files to start the three SourceJobs
  6. Collect every previews_ready notification and record its wall time
  7. Stop after --timeout seconds; report milestone times

Usage:
    python3 bench_first_image.py ~/Pictures [--timeout 120]
"""
import argparse
import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from statistics import mean, median, stdev
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Config resolution  (same logic as ConfigManager / main.py)
# ---------------------------------------------------------------------------

_REPO = os.path.dirname(__file__)

def _read_config() -> dict:
    try:
        import yaml
        with open(os.path.join(_REPO, "config.yaml")) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}

_CFG         = _read_config()
SOCKET_PATH  = _CFG.get("system", {}).get("socket_path",
                    f"/tmp/rabbitviewer_{os.getenv('USER', 'user')}.sock")
# Daemon stores the DB under files.cache.dir (default ~/.rabbitviewer/cache).
# Thumbnails are stored under cache_dir/thumbnails (default ~/.rabbitviewer/thumbnails).
# The DB rows hold absolute paths to thumbnail files, so we only need DB_PATH here.
_FILES_CACHE = os.path.expanduser(
    _CFG.get("files", {}).get("cache", {}).get("dir", "~/.rabbitviewer/cache")
)
DB_PATH      = os.path.join(_FILES_CACHE, "metadata.db")
DAEMON_SCRIPT = os.path.join(_REPO, "rabbitviewer_daemon.py")

def _find_python() -> str:
    venv = os.path.join(_REPO, "venv", "bin", "python")
    return venv if os.path.isfile(venv) else sys.executable

PYTHON = _find_python()

MILESTONES = [1, 5, 10, 25, 50, 100]

# ---------------------------------------------------------------------------
# Raw IPC helpers  (4-byte length-prefix + JSON, matching socket_client.py)
# ---------------------------------------------------------------------------

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def _send_recv(payload: dict, timeout: float = 10.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        msg = json.dumps(payload).encode()
        s.sendall(len(msg).to_bytes(4, "big") + msg)
        length = int.from_bytes(_recv_exactly(s, 4), "big")
        return json.loads(_recv_exactly(s, length))


def _wait_for_socket(deadline: float) -> bool:
    while time.perf_counter() < deadline:
        if os.path.exists(SOCKET_PATH):
            return True
        time.sleep(0.02)
    return False

# ---------------------------------------------------------------------------
# Cache purge for one directory
# ---------------------------------------------------------------------------

def purge_cache(directory: str) -> Tuple[int, int]:
    """
    Remove all thumbnail and view-image files for images under *directory*,
    and NULL out those paths in the DB.  Must be called while the daemon is
    down (no concurrent DB writes).

    Returns (files_purged, thumbnails_deleted).
    """
    directory = os.path.normpath(directory)
    if not os.path.exists(DB_PATH):
        return 0, 0

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # Fetch all rows whose file_path lives inside the target directory
        cur.execute(
            "SELECT file_path, thumbnail_path, view_image_path "
            "FROM image_metadata "
            "WHERE file_path LIKE ?",
            (directory + "/%",)
        )
        rows = cur.fetchall()
        deleted = 0
        for _, thumb, view in rows:
            for p in (thumb, view):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                        deleted += 1
                    except OSError:
                        pass
        # NULL out the cached paths so the daemon treats every file as uncached
        conn.execute(
            "UPDATE image_metadata "
            "SET thumbnail_path = NULL, view_image_path = NULL, "
            "    content_hash   = NULL, updated_at = ? "
            "WHERE file_path LIKE ?",
            (time.time(), directory + "/%")
        )
        conn.commit()
        return len(rows), deleted
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------

def kill_daemon(timeout: float = 6.0) -> None:
    if not os.path.exists(SOCKET_PATH):
        return
    try:
        _send_recv({"command": "shutdown"}, timeout=3.0)
    except Exception:
        pass
    deadline = time.perf_counter() + timeout
    while os.path.exists(SOCKET_PATH) and time.perf_counter() < deadline:
        time.sleep(0.05)


def start_daemon() -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, DAEMON_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=_REPO,
        start_new_session=True,
    )

# ---------------------------------------------------------------------------
# Notification collector  (mirrors NotificationListener, no Qt)
# ---------------------------------------------------------------------------

class NotificationCollector(threading.Thread):
    """
    Opens a persistent notification socket, registers as a listener, then
    puts every previews_ready entry (with a thumbnail_path) onto *out_q* as
    a (wall_seconds_from_t0, image_path) tuple.
    """
    def __init__(self, t0: float, out_q: "queue.Queue[Tuple[float, str]]") -> None:
        super().__init__(daemon=True)
        self.t0    = t0
        self.out_q = out_q
        self._ready = threading.Event()
        self._stop  = threading.Event()

    def run(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(SOCKET_PATH)
                hs = json.dumps({"type": "register_notifier"}).encode()
                s.sendall(len(hs).to_bytes(4, "big") + hs)
                self._ready.set()
                s.settimeout(0.5)
                while not self._stop.is_set():
                    try:
                        raw_len = _recv_exactly(s, 4)
                    except (socket.timeout, OSError):
                        continue
                    length = int.from_bytes(raw_len, "big")
                    try:
                        body = _recv_exactly(s, length)
                    except (socket.timeout, OSError):
                        continue
                    try:
                        msg = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "previews_ready":
                        data  = msg.get("data", {})
                        thumb = data.get("thumbnail_path")
                        path  = data.get("image_path", "")
                        if thumb:
                            self.out_q.put((time.perf_counter() - self.t0, path))
        except Exception:
            self._ready.set()   # unblock wait_ready even on failure

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("directory",
                    help="Directory to benchmark (e.g. ~/Pictures)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="Seconds to collect notifications (default: 120)")
    ap.add_argument("--no-recursive", dest="recursive",
                    action="store_false", default=True)
    args = ap.parse_args()

    directory = os.path.realpath(os.path.expanduser(args.directory))
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}", file=sys.stderr)
        sys.exit(1)

    print(f"Directory : {directory}")
    print(f"Socket    : {SOCKET_PATH}")
    print(f"DB        : {DB_PATH}")
    print(f"Timeout   : {args.timeout}s")
    print()

    # ── 1. Kill existing daemon ───────────────────────────────────────────
    print("Stopping any running daemon...", flush=True)
    kill_daemon()

    # ── 2. Purge cache for this directory ────────────────────────────────
    print(f"Purging cached thumbnails for {directory}...", flush=True)
    n_rows, n_deleted = purge_cache(directory)
    print(f"  {n_rows} DB rows found, {n_deleted} thumbnail/view files deleted")
    print()

    # ── 3. Start daemon; t=0 is the Popen call ───────────────────────────
    t0 = time.perf_counter()
    proc = start_daemon()
    print(f"Daemon PID {proc.pid} started (t=0).", flush=True)

    if not _wait_for_socket(t0 + 15.0):
        print("ERROR: daemon socket did not appear within 15 s", file=sys.stderr)
        proc.terminate()
        sys.exit(1)
    t_socket = time.perf_counter() - t0
    print(f"Socket ready: {t_socket*1000:.0f} ms", flush=True)

    # ── 4. Register notification listener ────────────────────────────────
    out_q: "queue.Queue[Tuple[float, str]]" = queue.Queue()
    collector = NotificationCollector(t0, out_q)
    collector.start()
    if not collector.wait_ready(timeout=5.0):
        print("ERROR: could not register notification listener", file=sys.stderr)
        proc.terminate()
        sys.exit(1)
    t_connected = time.perf_counter() - t0
    print(f"Notifier registered: {t_connected*1000:.0f} ms", flush=True)

    # ── 5. Send get_directory_files (triggers three SourceJobs) ──────────
    session_id = str(uuid.uuid4())
    t_scan_sent = time.perf_counter() - t0
    try:
        resp = _send_recv({
            "command":    "get_directory_files",
            "path":       directory,
            "recursive":  args.recursive,
            "session_id": session_id,
        })
    except Exception as exc:
        print(f"ERROR: get_directory_files failed: {exc}", file=sys.stderr)
        collector.stop()
        proc.terminate()
        sys.exit(1)
    t_scan_ack = time.perf_counter() - t0
    db_files = resp.get("files", [])
    print(f"Scan started: {t_scan_sent*1000:.0f} ms  "
          f"(ack: {t_scan_ack*1000:.0f} ms, {len(db_files)} files already in DB)")
    print()
    print("Collecting notifications... (Ctrl-C to stop early)")
    print()

    # ── 6. Collect notifications until timeout ───────────────────────────
    ready_times: List[float] = []
    deadline = time.perf_counter() + args.timeout
    last_print = -1

    while time.perf_counter() < deadline:
        try:
            t_ready, img_path = out_q.get(timeout=0.2)
        except queue.Empty:
            # Print a progress dot every 5 s so the user knows it's alive
            elapsed = int(time.perf_counter() - t0)
            if elapsed % 5 == 0 and elapsed != last_print:
                last_print = elapsed
                print(f"  {elapsed}s — {len(ready_times)} thumbnails so far", flush=True)
            continue

        ready_times.append(t_ready)
        n = len(ready_times)

        # Print every thumbnail that arrives in the first 10, then milestones
        if n <= 10 or n in MILESTONES:
            print(f"  [{n:4d}] {t_ready*1000:8.1f} ms  {os.path.basename(img_path)}")

    collector.stop()

    # ── 7. Shut down daemon ───────────────────────────────────────────────
    print()
    print("Benchmark complete. Shutting down daemon...", flush=True)
    kill_daemon()

    # ── 8. Report ─────────────────────────────────────────────────────────
    if not ready_times:
        print("No previews_ready notifications received.")
        return

    print()
    print("=" * 60)
    print("Timeline (wall clock from t=0 = daemon Popen):")
    print(f"  t_socket   : {t_socket*1000:8.1f} ms  daemon socket appeared")
    print(f"  t_notifier : {t_connected*1000:8.1f} ms  notification listener registered")
    print(f"  t_scan_sent: {t_scan_sent*1000:8.1f} ms  get_directory_files sent")
    print(f"  t_scan_ack : {t_scan_ack*1000:8.1f} ms  SourceJobs submitted")
    print()

    ready_sorted = sorted(ready_times)
    print("Time-till-Nth-thumbnail (from t=0):")
    for n in MILESTONES:
        if n <= len(ready_sorted):
            marker = "  ← first image" if n == 1 else ""
            print(f"  t_{n:<4d}: {ready_sorted[n-1]*1000:8.1f} ms{marker}")

    # Time after scan was acknowledged (removes startup noise)
    print()
    print("Processing latency (from SourceJobs submitted to notification):")
    for n in MILESTONES:
        if n <= len(ready_sorted):
            lag = (ready_sorted[n-1] - t_scan_ack) * 1000
            marker = "  ← first image" if n == 1 else ""
            print(f"  t_{n:<4d}: {lag:8.1f} ms{marker}")

    print()
    total = len(ready_times)
    elapsed_total = max(ready_times) - min(ready_times) if total > 1 else 0
    print(f"Total notifications received : {total}")
    if elapsed_total > 0 and total > 1:
        print(f"Throughput                   : {(total-1)/elapsed_total:.1f} thumbnails/s "
              f"(between first and last)")

    if total > 1:
        gaps = [(ready_sorted[i] - ready_sorted[i-1]) * 1000
                for i in range(1, min(total, 100))]
        print()
        print("Inter-notification gap (first 100):")
        print(f"  mean   : {mean(gaps):7.1f} ms")
        print(f"  median : {median(gaps):7.1f} ms")
        if len(gaps) > 1:
            print(f"  stdev  : {stdev(gaps):7.1f} ms")
        print(f"  max    : {max(gaps):7.1f} ms")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
