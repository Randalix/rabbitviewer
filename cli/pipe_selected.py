#!/usr/bin/env python3
"""Print selected image paths to stdout, one per line.

Usage:
    rabbit pipe-selected
    rabbit pipe-selected | xargs -I{} cp {} /tmp/export/
    rabbit pipe-selected | xargs open
    rabbit pipe-selected -0 | xargs -0 mogrify -resize 50%

Options:
    -0, --null    Separate paths with NUL instead of newline (for xargs -0).

Self-contained: only stdlib. No project imports, no pydantic.
"""

import json
import socket
import sys

GUI_SOCKET_PATH = "/tmp/rabbitviewer_gui.sock"


# ---------------------------------------------------------------------------
# Minimal framed-socket helpers (4-byte big-endian length prefix + UTF-8 JSON)
# ---------------------------------------------------------------------------

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Socket closed before all bytes received")
        data.extend(chunk)
    return bytes(data)


def _call(socket_path: str, payload: dict, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        data = json.dumps(payload).encode()
        sock.sendall(len(data).to_bytes(4, "big") + data)
        length = int.from_bytes(_recv_exactly(sock, 4), "big")
        return json.loads(_recv_exactly(sock, length).decode())


# ---------------------------------------------------------------------------
# GUI helper
# ---------------------------------------------------------------------------

def get_selection() -> list[str]:
    resp = _call(GUI_SOCKET_PATH, {"command": "get_selection"})
    raw = resp.get("paths", [])
    return [p["path"] if isinstance(p, dict) else p for p in raw]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
