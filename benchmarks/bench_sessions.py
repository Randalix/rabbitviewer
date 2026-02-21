"""
RabbitViewer log scraper — extracts per-session benchmark metrics.

Parses image_viewer.log (GUI) and optionally daemon.log to report:
  - When each GUI session started
  - Time to first image displayed (GUI-side)
  - Time-till-Nth-thumbnail milestones
  - GUI startup cost, scan ACK latency
  - Thumbnail throughput and inter-notification gaps
  - Dropped notifications (from daemon.log)
  - Session duration and exit code

Usage:
    python3 bench_log_scraper.py [--last N] [--dir FILTER] [--all]
    python3 bench_log_scraper.py --gui-log path/to/image_viewer.log
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from statistics import mean, median, stdev
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REPO = os.path.dirname(__file__)
GUI_LOG_DEFAULT    = os.path.join(_REPO, "image_viewer.log")
DAEMON_LOG_DEFAULT = os.path.join(_REPO, "daemon.log")

MILESTONES = [1, 5, 10, 25, 50, 100]

# Heuristic: if first thumbnail arrives within this many ms of scan ACK,
# treat the session as warm-cache (thumbnails already on disk).
WARM_CACHE_THRESHOLD_MS = 400

# ---------------------------------------------------------------------------
# Log timestamp parsing
# ---------------------------------------------------------------------------

_GUI_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(DEBUG|INFO|WARNING|ERROR)\] root - (.*)"
)
_DAEMON_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - .+? - (DEBUG|INFO|WARNING|ERROR) - (.*)"
)
_TS_FMT = "%Y-%m-%d %H:%M:%S,%f"


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, _TS_FMT)


def _ms(delta: timedelta) -> float:
    return delta.total_seconds() * 1000


# ---------------------------------------------------------------------------
# GUI log patterns
# ---------------------------------------------------------------------------

_P_GUI_START   = re.compile(r"Starting RabbitViewer GUI$")
_P_NOTIF_CONN  = re.compile(r"Notification client connected to daemon\.")
_P_SCAN_START  = re.compile(r"MainWindow: Starting to load directory: (.+?) \(Recursive: (True|False)\)")
_P_SCAN_ACK    = re.compile(r"Daemon acknowledged scan request for (.+?)\. Waiting")
_P_THUMB_READY = re.compile(r"ThumbnailViewWidget received notification: Previews ready for (.+)")
_P_APP_EXIT    = re.compile(r"Application exiting with code (\d+)")

# ---------------------------------------------------------------------------
# Session data class
# ---------------------------------------------------------------------------

class Session:
    __slots__ = (
        "t_start", "t_connected", "t_scan", "t_scan_ack",
        "directory", "recursive", "thumb_times",
        "t_exit", "exit_code", "dropped_notifs",
    )

    def __init__(self, t_start: datetime):
        self.t_start:      datetime           = t_start
        self.t_connected:  Optional[datetime] = None
        self.t_scan:       Optional[datetime] = None
        self.t_scan_ack:   Optional[datetime] = None
        self.directory:    Optional[str]      = None
        self.recursive:    Optional[bool]     = None
        self.thumb_times:  list               = []   # list of datetime
        self.t_exit:       Optional[datetime] = None
        self.exit_code:    Optional[int]      = None
        self.dropped_notifs: int              = 0    # filled from daemon.log


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_gui_log(path: str) -> list[Session]:
    sessions: list[Session] = []
    current: Optional[Session] = None

    with open(path, errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            m = _GUI_TS_RE.match(raw)
            if not m:
                continue
            ts  = _parse_ts(m.group(1))
            msg = m.group(3)

            if _P_GUI_START.search(msg):
                current = Session(ts)
                sessions.append(current)
                continue

            if current is None:
                continue

            if _P_NOTIF_CONN.search(msg):
                current.t_connected = ts

            elif (sm := _P_SCAN_START.search(msg)):
                current.directory = sm.group(1)
                current.recursive = sm.group(2) == "True"
                current.t_scan    = ts

            elif (sm := _P_SCAN_ACK.search(msg)):
                current.t_scan_ack = ts

            elif (sm := _P_THUMB_READY.search(msg)):
                current.thumb_times.append(ts)

            elif (sm := _P_APP_EXIT.search(msg)):
                current.t_exit  = ts
                current.exit_code = int(sm.group(1))

    return sessions


def annotate_dropped_notifs(sessions: list[Session], daemon_log: str) -> None:
    """Count 'Notification queue full' drops in daemon.log per session window."""
    if not os.path.exists(daemon_log):
        return

    # Build list of (start, end) windows
    windows = []
    for s in sessions:
        t0 = s.t_start
        t1 = s.t_exit or (s.thumb_times[-1] if s.thumb_times else t0 + timedelta(hours=1))
        windows.append([t0, t1, s])

    _DROP_RE = re.compile(r"Notification queue full")

    with open(daemon_log, errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not _DROP_RE.search(raw):
                continue
            m = _DAEMON_TS_RE.match(raw)
            if not m:
                continue
            ts = _parse_ts(m.group(1))
            for w in windows:
                if w[0] <= ts <= w[1]:
                    w[2].dropped_notifs += 1


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_ts(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M:%S.%f")[:-3] if dt else "—"


def report_session(s: Session, idx: int, total: int) -> None:
    print(f"{'='*64}")
    print(f"Session {idx}/{total}   started {s.t_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*64}")

    print(f"  Directory : {s.directory or '(none)'}"
          + (f"  [recursive]" if s.recursive else ""))

    # --- startup chain ---
    t0 = s.t_start
    def ms_since(dt: Optional[datetime]) -> Optional[float]:
        return _ms(dt - t0) if dt else None

    print()
    print("  Startup timeline (from GUI launch):")
    print(f"    t=0        GUI process start         {_fmt_ts(s.t_start)}")
    if s.t_connected:
        print(f"    +{ms_since(s.t_connected):6.0f} ms  Daemon socket connected       {_fmt_ts(s.t_connected)}")
    if s.t_scan:
        print(f"    +{ms_since(s.t_scan):6.0f} ms  Scan sent (load_directory)    {_fmt_ts(s.t_scan)}")
    if s.t_scan_ack:
        print(f"    +{ms_since(s.t_scan_ack):6.0f} ms  Scan acknowledged             {_fmt_ts(s.t_scan_ack)}")

    thumbs = s.thumb_times
    if not thumbs:
        print()
        print("  No thumbnails received this session.")
    else:
        t_ref = s.t_scan_ack or s.t_scan or t0
        warm = _ms(thumbs[0] - t_ref) < WARM_CACHE_THRESHOLD_MS
        cache_label = "warm cache" if warm else "cold / generating"

        print()
        print(f"  Time-till-Nth thumbnail — {cache_label}:")
        print(f"    {'N':>5}  {'from GUI launch':>16}  {'from scan sent':>14}  filename")
        t_scan_ref = s.t_scan or t0
        for n in MILESTONES:
            if n > len(thumbs):
                break
            dt_gui  = _ms(thumbs[n-1] - t0)
            dt_scan = _ms(thumbs[n-1] - t_scan_ref)
            lbl = "  ← first image" if n == 1 else ""
            print(f"    {n:>5}  {dt_gui:>14.0f} ms  {dt_scan:>12.0f} ms{lbl}")

        # throughput
        span_s = (thumbs[-1] - thumbs[0]).total_seconds()
        throughput = (len(thumbs) - 1) / span_s if span_s > 0 and len(thumbs) > 1 else None

        print()
        print(f"  Thumbnails received : {len(thumbs)}")
        if throughput:
            print(f"  Throughput          : {throughput:.1f} / s")

        # inter-notification gaps (first 100)
        if len(thumbs) > 1:
            sample = thumbs[:100]
            gaps = [_ms(sample[i] - sample[i-1]) for i in range(1, len(sample))]
            print(f"  Inter-notif gaps (first {len(gaps)}):")
            print(f"    mean {mean(gaps):.1f} ms  median {median(gaps):.1f} ms"
                  + (f"  stdev {stdev(gaps):.1f} ms" if len(gaps) > 1 else "")
                  + f"  max {max(gaps):.1f} ms")

    # dropped notifications
    if s.dropped_notifs:
        print(f"  Dropped notifs (daemon queue full) : {s.dropped_notifs}")

    # session duration
    if s.t_exit:
        dur = _ms(s.t_exit - t0) / 1000
        print(f"  Session duration    : {dur:.1f} s  (exit code {s.exit_code})")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--gui-log",    default=GUI_LOG_DEFAULT,
                    help="Path to image_viewer.log")
    ap.add_argument("--daemon-log", default=DAEMON_LOG_DEFAULT,
                    help="Path to daemon.log")
    ap.add_argument("--last", type=int, default=3, metavar="N",
                    help="Show the last N sessions (default: 3; 0 = all)")
    ap.add_argument("--all", action="store_true",
                    help="Show every session (same as --last 0)")
    ap.add_argument("--dir", metavar="FILTER",
                    help="Only show sessions whose directory contains FILTER")
    args = ap.parse_args()

    if not os.path.exists(args.gui_log):
        print(f"GUI log not found: {args.gui_log}", file=sys.stderr)
        sys.exit(1)

    sessions = parse_gui_log(args.gui_log)
    if not sessions:
        print("No sessions found in GUI log.")
        return

    annotate_dropped_notifs(sessions, args.daemon_log)

    # filter
    if args.dir:
        sessions = [s for s in sessions if s.directory and args.dir in s.directory]

    if not sessions:
        print("No matching sessions.")
        return

    # select
    limit = 0 if args.all else args.last
    shown = sessions if limit == 0 else sessions[-limit:]

    print(f"RabbitViewer session report — {len(sessions)} total sessions, showing {len(shown)}")
    print(f"GUI log    : {args.gui_log}")
    print(f"Daemon log : {args.daemon_log}")
    print()

    for i, s in enumerate(shown, start=len(sessions) - len(shown) + 1):
        report_session(s, i, len(sessions))


if __name__ == "__main__":
    main()
