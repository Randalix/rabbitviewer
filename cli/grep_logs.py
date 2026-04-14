#!/usr/bin/env python3
"""Search RabbitViewer logs for errors, warnings, and custom patterns.

Designed for AI-agent use: prints matching lines with context so an agent
can diagnose issues without needing shell access to the log files directly.

Default behaviour (no flags): show all ERROR and WARNING lines from both
the GUI log and daemon log, most-recent 200 lines each.

Usage:
    rabbit grep-logs
    rabbit grep-logs --tail 500
    rabbit grep-logs --pattern "thumbnail" --pattern "cache miss"
    rabbit grep-logs --gui-only
    rabbit grep-logs --daemon-only
    rabbit grep-logs --level DEBUG
    rabbit grep-logs --context 3

Options:
    --tail N            Lines to scan from the end of each log (default: 200).
    --pattern PATTERN   Extra pattern to match (can be given multiple times).
                        Always case-insensitive. Stacked with the default
                        ERROR/WARNING filter using OR logic.
    --level LEVEL       Minimum log level to include: DEBUG, INFO, WARNING,
                        ERROR, CRITICAL (default: WARNING).
    --gui-only          Only read ~/.rabbitviewer/rabbitviewer.log.
    --daemon-only       Only read ~/.rabbitviewer/daemon.log.
    --context N         Print N lines before and after each match (default: 0).
    --no-defaults       Suppress the default ERROR/WARNING filter; only apply
                        --pattern filters.
"""

import re
import sys
from collections import deque
from pathlib import Path

LOG_DIR = Path.home() / ".rabbitviewer"
GUI_LOG = LOG_DIR / "rabbitviewer.log"
DAEMON_LOG = LOG_DIR / "daemon.log"

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

__completions__ = [
    "--context",
    "--daemon-only",
    "--gui-only",
    "--level",
    "--no-defaults",
    "--pattern",
    "--tail",
]


def _parse_args(argv: list[str]) -> dict:
    args = {
        "tail": 200,
        "patterns": [],
        "level": "WARNING",
        "gui_only": False,
        "daemon_only": False,
        "context": 0,
        "no_defaults": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a == "--tail":
            args["tail"] = int(argv[i + 1]); i += 2
        elif a == "--pattern":
            args["patterns"].append(argv[i + 1]); i += 2
        elif a == "--level":
            args["level"] = argv[i + 1].upper(); i += 2
        elif a == "--gui-only":
            args["gui_only"] = True; i += 1
        elif a == "--daemon-only":
            args["daemon_only"] = True; i += 1
        elif a == "--context":
            args["context"] = int(argv[i + 1]); i += 2
        elif a == "--no-defaults":
            args["no_defaults"] = True; i += 1
        else:
            print(f"grep-logs: unknown option: {a}", file=sys.stderr)
            sys.exit(1)
    return args


def _level_pattern(min_level: str) -> re.Pattern | None:
    """Return a pattern matching any level >= min_level, or None for DEBUG."""
    try:
        idx = LEVELS.index(min_level)
    except ValueError:
        print(f"grep-logs: unknown level '{min_level}'. Use: {', '.join(LEVELS)}", file=sys.stderr)
        sys.exit(1)
    levels = LEVELS[idx:]
    if not levels or levels == LEVELS:
        return None
    joined = "|".join(levels)
    return re.compile(rf"\[({joined})\]")


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last n lines of path efficiently."""
    if not path.exists():
        return []
    buf: deque[str] = deque(maxlen=n)
    with path.open("r", errors="replace") as fh:
        for line in fh:
            buf.append(line.rstrip("\n"))
    return list(buf)


def _grep(lines: list[str], matchers: list[re.Pattern], context: int) -> list[str]:
    """Return lines (with context) that match any pattern in matchers."""
    if not matchers:
        return lines

    matched_indices: set[int] = set()
    for i, line in enumerate(lines):
        if any(m.search(line) for m in matchers):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                matched_indices.add(j)

    result = []
    prev = -2
    for i in sorted(matched_indices):
        if i > prev + 1 and result:
            result.append("---")
        result.append(lines[i])
        prev = i
    return result


def _print_section(label: str, lines: list[str]) -> None:
    print(f"\n=== {label} ({len(lines)} matching lines) ===")
    if not lines:
        print("  (no matches)")
    else:
        for line in lines:
            print(line)


def main() -> None:
    argv = sys.argv[1:]
    args = _parse_args(argv)

    matchers: list[re.Pattern] = []

    # Default level filter
    if not args["no_defaults"]:
        lvl_pat = _level_pattern(args["level"])
        if lvl_pat:
            matchers.append(lvl_pat)

    # User-supplied patterns (OR logic with level filter)
    for pat in args["patterns"]:
        matchers.append(re.compile(pat, re.IGNORECASE))

    logs: list[tuple[str, Path]] = []
    if not args["daemon_only"]:
        logs.append(("GUI log", GUI_LOG))
    if not args["gui_only"]:
        logs.append(("Daemon log", DAEMON_LOG))

    any_found = False
    for label, path in logs:
        if not path.exists():
            print(f"grep-logs: {label} not found at {path}", file=sys.stderr)
            continue
        lines = _tail_lines(path, args["tail"])
        matched = _grep(lines, matchers, args["context"])
        _print_section(f"{label}: {path}", matched)
        if matched and matched != ["(no matches)"]:
            any_found = True

    if not any_found:
        print("\nNo matches found.")


if __name__ == "__main__":
    main()
