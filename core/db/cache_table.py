"""Cache management queries: LRU eviction candidates.

DB queries only — no filesystem I/O.
"""

import logging
import sqlite3
from typing import List, Tuple

from core.db.base_table import BaseTable

logger = logging.getLogger(__name__)


class CacheTable(BaseTable):

    def get_cache_paths(self) -> List[Tuple[str, str]]:
        """Return (thumbnail_path, view_image_path) for all cached files."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
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
            conn = self._read_conn()
            cursor = conn.cursor()
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

    # close() inherited from BaseTable
