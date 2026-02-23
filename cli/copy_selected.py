#!/usr/bin/env python3
"""Copy selected images to a destination directory (skip existing).

Usage:
    rabbit copy-selected <destination_directory>

Workflow:
    1. Read the current selection from the running GUI.
    2. rsync each file to the destination, skipping files already up-to-date.
    3. Report results.

Self-contained: only stdlib + rsync. No project imports, no pydantic.
"""

import json
import socket
import subprocess
import sys
from pathlib import Path

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
    return resp.get("paths", [])


# ---------------------------------------------------------------------------
# rsync copy
# ---------------------------------------------------------------------------

def rsync_copy(sources: list[Path], dst: Path) -> subprocess.CompletedProcess:
    """Copy *sources* to *dst* via rsync, skipping files already up-to-date.

    Uses --ignore-existing so files already present at the destination are
    never overwritten or re-transferred.
    """
    cmd = [
        "rsync",
        "--progress",
        "--ignore-existing",
        *[str(s) for s in sources],
        str(dst) + "/",
    ]
    return subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: rabbit copy-selected <destination_directory>")
        sys.exit(1)

    dest = Path(sys.argv[1]).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    selected = get_selection()
    if not selected:
        print("No images selected.")
        sys.exit(0)

    sources = [Path(s).resolve() for s in selected]
    print(f"Copying {len(sources)} image(s) to {dest} ...")

    try:
        rsync_copy(sources, dest)
    except subprocess.CalledProcessError as e:
        print(f"rsync failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. {len(sources)} image(s) copied (existing files skipped).")


if __name__ == "__main__":
    main()
