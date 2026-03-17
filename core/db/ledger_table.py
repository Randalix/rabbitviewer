"""Ledger tables: scan_ledger, file_work, pending_writes, file_transfers.

Each ledger tracks intent/progress for crash recovery and daemon resumption.
"""

import json
import logging
import os
import sqlite3
import time
from typing import List

from core.db.base_table import BaseTable

logger = logging.getLogger(__name__)


class LedgerTable(BaseTable):

    # ------------------------------------------------------------------
    #  Write-intent ledger (pending_writes)
    # ------------------------------------------------------------------

    def pending_write_exists(self, file_path: str, write_type: str) -> bool:
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT 1 FROM pending_writes WHERE file_path = ? AND write_type = ?',
                (file_path, write_type))
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"pending_write_exists failed: {e}")
            return False

    def pending_write_insert(self, file_path: str, write_type: str, payload: dict) -> None:
        """Record a file-write intent. INSERT OR REPLACE so the latest value wins."""
        try:
            with self._lock:
                self.conn.execute(
                    'INSERT OR REPLACE INTO pending_writes '
                    '(file_path, write_type, payload, created_at) VALUES (?, ?, ?, ?)',
                    (file_path, write_type, json.dumps(payload), time.time()),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"pending_write_insert failed: {e}")

    def pending_write_remove(self, file_path: str, write_type: str, payload: dict) -> None:
        """Delete a completed write intent only if the payload matches."""
        try:
            with self._lock:
                self.conn.execute(
                    'DELETE FROM pending_writes '
                    'WHERE file_path = ? AND write_type = ? AND payload = ?',
                    (file_path, write_type, json.dumps(payload)),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"pending_write_remove failed: {e}")

    def pending_write_get_all(self) -> List[dict]:
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                'SELECT file_path, write_type, payload FROM pending_writes'
            )
            results = []
            for row in cursor.fetchall():
                try:
                    payload = json.loads(row[2])
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error("Skipping corrupt pending_write row %s: %s", row[0], e)
                    continue
                results.append({'file_path': row[0], 'write_type': row[1],
                                'payload': payload})
            return results
        except sqlite3.Error as e:
            logger.error(f"pending_write_get_all failed: {e}")
            return []

    def delete_for_files(self, file_paths: List[str]) -> None:
        """Delete pending_writes for the given file paths."""
        if not file_paths:
            return
        try:
            placeholders = ','.join('?' for _ in file_paths)
            with self._lock:
                self.conn.execute(
                    f'DELETE FROM pending_writes WHERE file_path IN ({placeholders})',
                    file_paths,
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"ledger delete_for_files failed: {e}")

    # ------------------------------------------------------------------
    #  Scan ledger (scan_ledger)
    # ------------------------------------------------------------------

    def ledger_batch_insert(self, file_paths: List[str], scan_root: str) -> None:
        """Record discovered files. INSERT OR IGNORE keeps existing entries."""
        if not file_paths:
            return
        try:
            now = time.time()
            with self._lock:
                self.conn.executemany(
                    'INSERT OR IGNORE INTO scan_ledger (file_path, scan_root, status, discovered_at) '
                    'VALUES (?, ?, ?, ?)',
                    [(fp, scan_root, 'discovered', now) for fp in file_paths],
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"ledger_batch_insert failed: {e}")

    def ledger_mark_complete(self, file_path: str) -> None:
        """Mark a file as fully processed."""
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE scan_ledger SET status = 'complete' WHERE file_path = ?",
                    (file_path,),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"ledger_mark_complete failed: {e}")

    def ledger_get_incomplete(self, scan_root: str) -> List[str]:
        """Return file paths that were discovered but not yet processed."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT file_path FROM scan_ledger "
                "WHERE scan_root = ? AND status = 'discovered'",
                (scan_root,),
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"ledger_get_incomplete failed: {e}")
            return []

    def ledger_get_walked_dirs(self, scan_root: str) -> set:
        """Derive the set of fully-walked directories from the ledger."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT DISTINCT file_path FROM scan_ledger "
                "WHERE scan_root = ? AND status = 'complete'",
                (scan_root,),
            )
            return {os.path.dirname(row[0]) for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logger.error(f"ledger_get_walked_dirs failed: {e}")
            return set()

    def ledger_prune_complete(self, scan_root: str) -> int:
        """Remove fully-processed entries for a scan root."""
        try:
            with self._lock:
                cursor = self.conn.execute(
                    "DELETE FROM scan_ledger WHERE scan_root = ? AND status = 'complete'",
                    (scan_root,),
                )
                self.conn.commit()
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error(f"ledger_prune_complete failed: {e}")
            return 0

    def ledger_get_all_scan_roots(self) -> List[str]:
        """Return all scan roots that have ledger entries."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT DISTINCT scan_root FROM scan_ledger"
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"ledger_get_all_scan_roots failed: {e}")
            return []

    def ledger_reset_all(self) -> int:
        """Delete all scan_ledger entries (used during cache migration)."""
        try:
            with self._lock:
                cursor = self.conn.execute("DELETE FROM scan_ledger")
                self.conn.commit()
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error("ledger_reset_all failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    #  Work-intent ledger (file_work)
    # ------------------------------------------------------------------

    def file_work_batch_insert(self, file_paths: List[str], work_type: str,
                               scan_root: str) -> None:
        """Record pending work. INSERT OR IGNORE — idempotent."""
        if not file_paths:
            return
        now = time.time()
        rows = [(fp, work_type, scan_root, now) for fp in file_paths]
        try:
            with self._lock:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO file_work "
                    "(file_path, work_type, scan_root, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error("file_work_batch_insert failed: %s", e)

    def file_work_remove(self, file_path: str, work_type: str) -> None:
        """Remove a single completed work entry."""
        try:
            with self._lock:
                self.conn.execute(
                    "DELETE FROM file_work WHERE file_path = ? AND work_type = ?",
                    (file_path, work_type),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error("file_work_remove failed: %s", e)

    def file_work_batch_remove(self, file_paths: List[str], work_type: str) -> None:
        """Remove completed work entries in batch."""
        if not file_paths:
            return
        try:
            with self._lock:
                self.conn.executemany(
                    "DELETE FROM file_work WHERE file_path = ? AND work_type = ?",
                    [(fp, work_type) for fp in file_paths],
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error("file_work_batch_remove failed: %s", e)

    def file_work_get_pending(self, scan_root: str, work_type: str) -> List[str]:
        """Return file paths with pending work of the given type."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT file_path FROM file_work "
                "WHERE scan_root = ? AND work_type = ?",
                (scan_root, work_type),
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("file_work_get_pending failed: %s", e)
            return []

    def file_work_get_all_roots(self) -> List[str]:
        """Return distinct scan roots that have pending work."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT DISTINCT scan_root FROM file_work"
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("file_work_get_all_roots failed: %s", e)
            return []

    def file_work_get_pending_types(self, scan_root: str) -> List[str]:
        """Return distinct work types with pending entries for a scan root."""
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT DISTINCT work_type FROM file_work "
                "WHERE scan_root = ?",
                (scan_root,),
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("file_work_get_pending_types failed: %s", e)
            return []

    def file_work_clear_all(self) -> int:
        """Delete all file_work entries (used during cache migration)."""
        try:
            with self._lock:
                cursor = self.conn.execute("DELETE FROM file_work")
                self.conn.commit()
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error("file_work_clear_all failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    #  File transfer ledger (file_transfers)
    # ------------------------------------------------------------------

    def file_transfer_batch_insert(self, file_paths: List[str],
                                   dest_dir: str, operation: str) -> None:
        """INSERT OR IGNORE — idempotent."""
        if not file_paths:
            return
        now = time.time()
        rows = [(fp, dest_dir, operation, "pending", now) for fp in file_paths]
        try:
            with self._lock:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO file_transfers "
                    "(source_path, dest_dir, operation, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error("file_transfer_batch_insert failed: %s", e)

    def file_transfer_mark_complete(self, source_path: str, dest_dir: str,
                                    operation: str, status: str) -> None:
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE file_transfers SET status = ? "
                    "WHERE source_path = ? AND dest_dir = ? AND operation = ?",
                    (status, source_path, dest_dir, operation),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error("file_transfer_mark_complete failed: %s", e)

    def file_transfer_get_pending(self, dest_dir: str,
                                  operation: str) -> List[str]:
        try:
            conn = self._read_conn()
            cursor = conn.execute(
                "SELECT source_path FROM file_transfers "
                "WHERE dest_dir = ? AND operation = ? AND status = 'pending'",
                (dest_dir, operation),
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("file_transfer_get_pending failed: %s", e)
            return []

    def file_transfer_cleanup_completed(self, max_age_seconds: float = 86400) -> int:
        cutoff = time.time() - max_age_seconds
        try:
            with self._lock:
                cursor = self.conn.execute(
                    "DELETE FROM file_transfers "
                    "WHERE status != 'pending' AND created_at < ?",
                    (cutoff,),
                )
                self.conn.commit()
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error("file_transfer_cleanup_completed failed: %s", e)
            return 0

    # close() inherited from BaseTable
