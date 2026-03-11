#!/usr/bin/env python3
"""Copy selected images to a destination directory (skip existing).

Usage:
    rabbit copy-selected <destination_directory>

Workflow:
    1. Read the current selection from the running GUI.
    2. rsync each file to the destination, skipping files already up-to-date.
    3. Report results.
"""

import subprocess
import sys
from pathlib import Path

from cli._socket import GUI_SOCKET_PATH, call


def get_selection() -> list[str]:
    resp = call(GUI_SOCKET_PATH, {"command": "get_selection"})
    raw = resp.get("paths", [])
    return [p["path"] if isinstance(p, dict) else p for p in raw]


# ---------------------------------------------------------------------------
# rsync copy
# ---------------------------------------------------------------------------

def rsync_copy(sources: list[Path], dst: Path) -> subprocess.CompletedProcess:
    """Uses --ignore-existing so files already present are never overwritten."""
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
