"""One-shot backfill: populate content_hash for files that have thumbnails but no hash."""
import hashlib
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_PATH = Path.home() / ".rabbitviewer" / "cache" / "metadata.db"
CHUNK = 256 * 1024  # match VolumeProber: hash first 256 KB only
WORKERS = 4


def hash_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:  # disk-io: read source file header for MD5
            data = f.read(CHUNK)
        return hashlib.md5(data).hexdigest()
    except OSError:
        return None


def run(directory: str):
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    prefix = directory.rstrip("/") + "/"
    rows = conn.execute(
        "SELECT file_path FROM image_metadata WHERE file_path LIKE ? AND content_hash IS NULL",
        (prefix + "%",),
    ).fetchall()

    paths = [r[0] for r in rows]
    total = len(paths)
    print(f"Backfilling {total} files under {directory}")
    if not total:
        return

    done = 0
    errors = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(hash_file, p): p for p in paths}
        for fut in as_completed(futures):
            path = futures[fut]
            h = fut.result()
            done += 1
            if h:
                conn.execute(
                    "UPDATE image_metadata SET content_hash = ?, updated_at = ? WHERE file_path = ?",
                    (h, time.time(), path),
                )
                if done % 200 == 0:
                    conn.commit()
                    elapsed = time.time() - start
                    rate = done / elapsed
                    remaining = (total - done) / rate if rate else 0
                    print(f"  {done}/{total}  ({rate:.0f}/s, ~{remaining:.0f}s left)")
            else:
                errors += 1

    conn.commit()
    conn.close()
    elapsed = time.time() - start
    print(f"Done: {done - errors}/{total} hashed in {elapsed:.1f}s ({errors} errors)")


if __name__ == "__main__":
    directory = sys.argv[1] if len(sys.argv) > 1 else "/Volumes/storage/pictures/2015"
    run(directory)
