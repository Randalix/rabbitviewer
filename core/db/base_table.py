"""Base class for per-domain table helpers.

Provides a write connection + lock for mutations and thread-local read
connections so readers never block on the write lock (SQLite WAL).
"""

import logging
import sqlite3
import threading
import time
from threading import Lock

from core.db.connection import create_connection

logger = logging.getLogger(__name__)


class BaseTable:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self.conn = create_connection(db_path)
        self._lock = Lock()
        self._local = threading.local()

    def _soft_commit(self):
        """Commit the current transaction.  Must be called with ``_lock`` held.

        With ``synchronous=NORMAL`` (set in ``create_connection``), this is
        cheap: SQLite skips the per-commit fsync to the WAL file, making
        high-frequency commits during bulk indexing far less expensive.

        Kept as a separate method so call sites document intent (batchable
        write) and a future coalescing strategy can be dropped in without
        touching every caller.
        """
        self.conn.commit()

    def _read_conn(self) -> sqlite3.Connection:
        """Return a thread-local read connection (WAL allows concurrent reads)."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = create_connection(self._db_path)
            self._local.conn = conn
        return conn

    def mark_ai_scanned(self, file_path: str, model_type: str) -> None:
        """Record that a file was processed by an AI model (success or failure).

        Uses _soft_commit so bulk indexing doesn't pay per-file fsync overhead.
        model_type convention: 'face:<model_name>' or 'clip:<model_name>'.
        """
        try:
            with self._lock:
                self.conn.execute(
                    'INSERT OR IGNORE INTO ai_scanned (file_path, model_type, scanned_at) '
                    'VALUES (?, ?, ?)',
                    (file_path, model_type, time.time()),
                )
                self._soft_commit()
        except sqlite3.Error as e:
            logger.error("mark_ai_scanned failed for %s/%s: %s", file_path, model_type, e)

    def close(self):
        with self._lock:
            self.conn.close()
        # Best-effort close of the calling thread's read connection.
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
