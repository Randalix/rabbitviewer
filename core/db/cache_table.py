"""Cache management queries: LRU tracking and eviction candidates.

DB queries only — no filesystem I/O.
"""

import logging
import sqlite3
import time
from threading import Lock
from typing import List, Tuple

from core.db.connection import create_connection

logger = logging.getLogger(__name__)


class CacheTable:
    def __init__(self, db_path: str):
        self.conn = create_connection(db_path)
        self._lock = Lock()

    def touch_accessed_at(self, file_path: str) -> None:
        """Update LRU timestamp. Best-effort — failures degrade gracefully."""
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE image_metadata SET accessed_at = ? WHERE file_path = ?",
                    (time.time(), file_path),
                )
                self.conn.commit()
        except sqlite3.Error:
            pass

    def get_cache_paths(self) -> List[Tuple[str, str]]:
        """Return (thumbnail_path, view_image_path) for all cached files."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT thumbnail_path, view_image_path FROM image_metadata
                    WHERE thumbnail_path IS NOT NULL OR view_image_path IS NOT NULL
                ''')
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error querying cache paths: {e}")
            return []

    def get_eviction_candidates(self) -> List[Tuple[str, str, str]]:
        """Return (file_path, thumbnail_path, view_image_path) ordered by accessed_at ASC."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT file_path, thumbnail_path, view_image_path
                    FROM image_metadata
                    WHERE thumbnail_path IS NOT NULL OR view_image_path IS NOT NULL
                    ORDER BY accessed_at ASC
                ''')
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error querying eviction candidates: {e}")
            return []

    def close(self):
        with self._lock:
            self.conn.close()
