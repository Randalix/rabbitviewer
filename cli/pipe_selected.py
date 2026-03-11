#!/usr/bin/env python3
"""Print selected image paths to stdout, one per line.

Usage:
    rabbit pipe-selected
    rabbit pipe-selected | xargs -I{} cp {} /tmp/export/
    rabbit pipe-selected | xargs open
    rabbit pipe-selected -0 | xargs -0 mogrify -resize 50%

Options:
    -0, --null    Separate paths with NUL instead of newline (for xargs -0).
"""

import sys

from cli._socket import GUI_SOCKET_PATH, call


def get_selection() -> list[str]:
    resp = call(GUI_SOCKET_PATH, {"command": "get_selection"})
    raw = resp.get("paths", [])
    return [p["path"] if isinstance(p, dict) else p for p in raw]


def main():
    null_sep = "-0" in sys.argv or "--null" in sys.argv

    try:
        selected = get_selection()
    except (ConnectionRefusedError, FileNotFoundError):
        print("RabbitViewer GUI is not running.", file=sys.stderr)
        sys.exit(1)

    if not selected:
        sys.exit(0)

    sep = "\0" if null_sep else "\n"
    sys.stdout.write(sep.join(selected))
    if not null_sep:
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
