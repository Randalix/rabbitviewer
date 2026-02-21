"""
Shared benchmark utilities for RabbitViewer.

Provides cache-clearing helpers so benchmarks can measure cold-start
performance without cached metadata or thumbnails.
"""
import os
import sqlite3
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Config resolution (mirrors bench_first_image.py / ConfigManager)
# ---------------------------------------------------------------------------

_REPO = os.path.join(os.path.dirname(__file__), os.pardir)


def _read_config() -> dict:
    try:
        import yaml
        with open(os.path.join(_REPO, "config.yaml")) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


_CFG = _read_config()

SOCKET_PATH = _CFG.get("system", {}).get(
    "socket_path", f"/tmp/rabbitviewer_{os.getenv('USER', 'user')}.sock"
)

_FILES_CACHE = os.path.expanduser(
    _CFG.get("files", {}).get("cache", {}).get("dir", "~/.rabbitviewer/cache")
)

DB_PATH = os.path.join(_FILES_CACHE, "metadata.db")

THUMBNAILS_DIR = os.path.expanduser(
    _CFG.get("files", {}).get("thumbnails", {}).get("dir", "~/.rabbitviewer/thumbnails")
)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def cold_cache(directory: Optional[str] = None) -> Tuple[int, int]:
    """
    Delete metadata rows (and their on-disk thumbnails) so the daemon must
    re-extract everything from scratch.

    Args:
        directory: If given, only purge rows whose file_path starts with this
                   directory.  If None, purge the *entire* database.

    Returns:
        (rows_deleted, thumbnail_files_deleted)

    Must be called while the daemon is **not running** — there is no locking
    against a live SQLite connection in the daemon process.
    """
    if not os.path.exists(DB_PATH):
        return 0, 0

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # Fetch cache-file paths before deleting rows.
        if directory:
            directory = os.path.normpath(directory)
            cur.execute(
                "SELECT thumbnail_path, view_image_path FROM image_metadata "
                "WHERE file_path LIKE ?",
                (directory + "/%",),
            )
        else:
            cur.execute(
                "SELECT thumbnail_path, view_image_path FROM image_metadata"
            )

        rows = cur.fetchall()
        files_deleted = 0
        for thumb, view in rows:
            for p in (thumb, view):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                        files_deleted += 1
                    except OSError:
                        pass

        # Delete the rows themselves so metadata is fully cold.
        if directory:
            cur.execute(
                "DELETE FROM image_metadata WHERE file_path LIKE ?",
                (directory + "/%",),
            )
        else:
            cur.execute("DELETE FROM image_metadata")

        conn.commit()
        return cur.rowcount, files_deleted
    finally:
        conn.close()
