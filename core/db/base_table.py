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
