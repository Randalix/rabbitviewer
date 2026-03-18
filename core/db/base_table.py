"""Base class for per-domain table helpers.

Provides a write connection + lock for mutations and thread-local read
connections so readers never block on the write lock (SQLite WAL).
"""

import sqlite3
import threading
from threading import Lock

from core.db.connection import create_connection


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
