"""Bookmark copy/move with rsync-va skip semantics and DB tracking."""
import logging
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from core.enums import FileOperation

if TYPE_CHECKING:
    from core.metadata_database import MetadataDatabase

import yaml

logger = logging.getLogger(__name__)

BOOKMARKS_PATH = os.path.expanduser("~/.rabbitviewer/bookmarks.yaml")


@dataclass(frozen=True)
class Bookmark:
    key: str
    name: str
    path: str


def load_bookmarks() -> List[Bookmark]:
    try:
        with open(BOOKMARKS_PATH, "r") as f:  # disk-io: bookmark config load
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.warning("Failed to load bookmarks: %s", e)
        return []

    result = []
    for entry in data.get("bookmarks", []):
        key = entry.get("key", "")
        name = entry.get("name", "")
        path = os.path.expanduser(entry.get("path", ""))
        if key and path:
            result.append(Bookmark(key=key, name=name or path, path=path))
    return result


def _should_transfer(src: str, dst: str) -> bool:
    if not os.path.exists(dst):  # disk-io: check dest exists for rsync skip
        return True
    try:
        src_stat = os.stat(src)  # disk-io: compare size+mtime for rsync skip
        dst_stat = os.stat(dst)  # disk-io: compare size+mtime for rsync skip
        if src_stat.st_size != dst_stat.st_size:
            return True
        # Compare mtime with 1-second tolerance (FAT/network fs granularity)
        if abs(src_stat.st_mtime - dst_stat.st_mtime) > 1.0:
            return True
    except OSError:
        return True
    return False


def transfer_file(src: str, dest_dir: str, move: bool = False) -> str:
    """Returns "copied", "moved", "skipped", or "error:<message>"."""
    basename = os.path.basename(src)
    dst = os.path.join(dest_dir, basename)

    if not os.path.isfile(src):  # disk-io: verify source exists before transfer
        return f"error:source not found: {src}"

    if not _should_transfer(src, dst):
        return "skipped"

    try:
        os.makedirs(dest_dir, exist_ok=True)
        if move:
            shutil.move(src, dst)  # disk-io: move source file to bookmark destination
            return "moved"
        else:
            shutil.copy2(src, dst)  # disk-io: copy source file to bookmark destination
            return "copied"
    except OSError as e:
        return f"error:{e}"


def execute_bookmark_transfer(
    file_paths: List[str],
    dest_dir: str,
    move: bool,
    db: Optional["MetadataDatabase"] = None,
) -> dict:
    """DB records survive GUI closure so the daemon can retry incomplete transfers."""
    dest_dir = os.path.expanduser(dest_dir)
    op = FileOperation.MOVE.value if move else FileOperation.COPY.value
    results = {"copied": 0, "moved": 0, "skipped": 0, "errors": []}

    if db:
        db.ledgers.file_transfer_batch_insert(file_paths, dest_dir, op)

    for src in file_paths:
        status = transfer_file(src, dest_dir, move=move)

        if status in ("copied", "moved", "skipped"):
            results[status] += 1
            if db:
                db.ledgers.file_transfer_mark_complete(src, dest_dir, op, status)
        else:
            results["errors"].append(status)
            if db:
                db.ledgers.file_transfer_mark_complete(src, dest_dir, op, "failed")

    total = len(file_paths)
    done = results["copied"] + results["moved"]
    skip = results["skipped"]
    errs = len(results["errors"])
    logger.info(
        "Bookmark %s to %s: %d done, %d skipped, %d errors (of %d)",
        op, dest_dir, done, skip, errs, total,
    )
    return results


def save_bookmarks(bookmarks: List[Bookmark]) -> None:
    data = {
        "bookmarks": [
            {"key": b.key, "name": b.name, "path": b.path} for b in bookmarks
        ]
    }
    try:
        os.makedirs(os.path.dirname(BOOKMARKS_PATH), exist_ok=True)
        with open(BOOKMARKS_PATH, "w") as f:  # disk-io: write bookmarks config
            yaml.dump(data, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to save bookmarks: %s", e)


def add_bookmark(path: str) -> None:
    path = os.path.expanduser(path)
    if not os.path.isdir(path):  # disk-io: verify directory exists before bookmarking
        logger.warning("Cannot bookmark non-existent directory: %s", path)
        return

    bookmarks = load_bookmarks()

    # Check if already bookmarked
    if any(b.path == path for b in bookmarks):
        logger.info("Path already bookmarked: %s", path)
        return

    # Find next available key
    used_keys = {int(b.key) for b in bookmarks if b.key.isdigit()}
    next_key = 1
    while next_key in used_keys:
        next_key += 1

    name = os.path.basename(path) or path
    new_bookmark = Bookmark(key=str(next_key), name=name, path=path)

    bookmarks.append(new_bookmark)
    save_bookmarks(bookmarks)
    logger.info("Added bookmark: %s", path)


def remove_bookmark(path: str) -> None:
    path = os.path.expanduser(path)
    bookmarks = load_bookmarks()

    original_count = len(bookmarks)
    bookmarks = [b for b in bookmarks if b.path != path]

    if len(bookmarks) < original_count:
        save_bookmarks(bookmarks)
        logger.info("Removed bookmark: %s", path)
    else:
        logger.warning("Attempted to remove non-existent bookmark: %s", path)

