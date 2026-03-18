"""SQLite connection factory with WAL mode and synchronous=NORMAL."""

import sqlite3


def create_connection(db_path: str) -> sqlite3.Connection:
    """Create a WAL-mode connection suitable for multi-threaded use.

    Each table class should call this once to get its own connection,
    enabling concurrent reads across domains via SQLite WAL.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
