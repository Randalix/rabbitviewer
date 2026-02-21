#!/usr/bin/env python3
"""Move selected images to a destination directory.

Usage:
    python move_selected.py <destination_directory>

Workflow:
    1. Read the current selection from the running GUI.
    2. rsync each file to the destination (with progress), then delete the source.
    3. Update the daemon's database with the new paths.
    4. Remove the images from the GUI view.

Self-contained: only stdlib + rsync. No project imports, no pydantic.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

GUI_SOCKET_PATH    = "/tmp/rabbitviewer_gui.sock"
DAEMON_SOCKET_PATH = f"/tmp/rabbitviewer_{os.getenv('USER', 'user')}.sock"


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
# GUI / daemon helpers
# ---------------------------------------------------------------------------

def get_selection() -> list[str]:
    resp = _call(GUI_SOCKET_PATH, {"command": "get_selection"})
    return resp.get("paths", [])


def remove_images(paths: list[str]) -> None:
    _call(GUI_SOCKET_PATH, {"command": "remove_images", "paths": paths})


def move_records(moves: list[dict]) -> int:
    resp = _call(DAEMON_SOCKET_PATH, {"command": "move_records", "moves": moves})
    return resp.get("moved_count", 0)


# ---------------------------------------------------------------------------
# rsync move
# ---------------------------------------------------------------------------

def rsync_move(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* via rsync with per-file progress, then remove *src*."""
    subprocess.run(
        ["rsync", "--progress", "--remove-source-files", str(src), str(dst)],
        check=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python move_selected.py <destination_directory>")
        sys.exit(1)

    dest = Path(sys.argv[1]).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    selected = get_selection()
    if not selected:
        print("No images selected.")
        sys.exit(0)

    moves = []
    errors = []
    for i, src in enumerate(selected, 1):
        src_path = Path(src).resolve()
        dst_path = dest / src_path.name
        print(f"[{i}/{len(selected)}] {src_path.name}")
        try:
            rsync_move(src_path, dst_path)
            moves.append({"old_path": str(src_path), "new_path": str(dst_path)})
        except subprocess.CalledProcessError as e:
            errors.append((src, e))
            print(f"  rsync failed for {src}: {e}")

    if moves:
        try:
            count = move_records(moves)
            print(f"Daemon DB updated: {count} record(s) moved.")
        except Exception as e:
            print(f"Warning: could not update daemon DB: {e}")

        try:
            remove_images([m["old_path"] for m in moves])
        except Exception as e:
            print(f"Warning: could not remove images from GUI: {e}")

    print(f"Moved {len(moves)} image(s) to {dest}.", end="")
    if errors:
        print(f" {len(errors)} error(s).")
    else:
        print()


if __name__ == "__main__":
    main()
