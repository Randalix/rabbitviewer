#!/usr/bin/env python3
"""Move selected images to a destination directory.

Usage:
    python move_selected.py <destination_directory>

Workflow:
    1. Read the current selection from the running GUI.
    2. rsync each file to the destination (with progress), then delete the source.
    3. Update the daemon's database with the new paths.
    4. Remove the images from the GUI view.
"""

import subprocess
import sys
from pathlib import Path

from cli._socket import GUI_SOCKET_PATH, call


def get_selection() -> list[str]:
    resp = call(GUI_SOCKET_PATH, {"command": "get_selection"})
    raw = resp.get("paths", [])
    return [p["path"] if isinstance(p, dict) else p for p in raw]


def remove_images(paths: list[str]) -> None:
    call(GUI_SOCKET_PATH, {"command": "remove_images", "paths": paths})


def move_records(moves: list[dict]) -> int:
    resp = call(GUI_SOCKET_PATH, {"command": "move_records", "moves": moves})
    return resp.get("moved_count", 0)


# ---------------------------------------------------------------------------
# rsync move
# ---------------------------------------------------------------------------

def rsync_move(src: Path, dst: Path) -> None:
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
            moves.append({"old_entry": {"path": str(src_path)}, "new_entry": {"path": str(dst_path)}})
        except subprocess.CalledProcessError as e:
            errors.append((src, e))
            print(f"  rsync failed for {src}: {e}")
            continue

        # Move XMP sidecar alongside the image (non-fatal)
        xmp_src = src_path.with_suffix(".xmp")
        if xmp_src.exists():
            try:
                rsync_move(xmp_src, dest / xmp_src.name)
            except subprocess.CalledProcessError as e:
                print(f"  Warning: sidecar move failed for {xmp_src.name}: {e}")

    if moves:
        try:
            count = move_records(moves)
            print(f"Daemon DB updated: {count} record(s) moved.")
        except Exception as e:  # why: GUI socket may be gone if user quit during move
            print(f"Warning: could not update daemon DB: {e}")

        try:
            remove_images([m["old_entry"]["path"] for m in moves])
        except Exception as e:  # why: GUI socket may be gone if user quit during move
            print(f"Warning: could not remove images from GUI: {e}")

    print(f"Moved {len(moves)} image(s) to {dest}.", end="")
    if errors:
        print(f" {len(errors)} error(s).")
    else:
        print()


if __name__ == "__main__":
    main()
