"""OcioTable: CRUD for ocio_assignments.

Supports per-file and per-folder OCIO assignments.  The get_for_file()
method walks the path hierarchy to find the nearest enclosing folder
assignment when no file-level entry exists.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Optional

from core.db.base_table import BaseTable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcioAssignment:
    path: str
    is_folder: bool
    config_path: str
    input_space: str
    display: str = ""
    view: str = ""


class OcioTable(BaseTable):

    def set(
        self,
        path: str,
        is_folder: bool,
        config_path: str,
        input_space: str,
        display: str = "",
        view: str = "",
    ) -> None:
        """Upsert an OCIO assignment for a file or folder path."""
        try:
            with self._lock:
                self.conn.execute(
                    """
                    INSERT INTO ocio_assignments
                        (path, is_folder, config_path, input_space, display, view, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        is_folder   = excluded.is_folder,
                        config_path = excluded.config_path,
                        input_space = excluded.input_space,
                        display     = excluded.display,
                        view        = excluded.view,
                        updated_at  = excluded.updated_at
                    """,
                    (path, int(is_folder), config_path, input_space, display, view, time.time()),
                )
                self._soft_commit()
        except sqlite3.Error as e:
            logger.error("ocio_table.set failed for %s: %s", path, e)

    def delete(self, path: str) -> None:
        """Remove the OCIO assignment for *path* (exact match only)."""
        try:
            with self._lock:
                self.conn.execute(
                    "DELETE FROM ocio_assignments WHERE path = ?", (path,)
                )
                self._soft_commit()
        except sqlite3.Error as e:
            logger.error("ocio_table.delete failed for %s: %s", path, e)

    def get_for_file(self, file_path: str) -> Optional[OcioAssignment]:
        """Return the effective OCIO assignment for *file_path*.

        Checks in order:
        1. An exact file-level assignment.
        2. The longest-matching folder assignment among all ancestor paths.
        """
        try:
            conn = self._read_conn()

            # 1. Exact file match
            row = conn.execute(
                "SELECT path, is_folder, config_path, input_space, display, view "
                "FROM ocio_assignments WHERE path = ? AND is_folder = 0",
                (file_path,),
            ).fetchone()
            if row:
                return OcioAssignment(row[0], bool(row[1]), *row[2:])

            # 2. Longest-matching folder ancestor
            ancestors = [str(p) for p in PurePosixPath(file_path).parents]
            if not ancestors:
                return None
            placeholders = ",".join("?" * len(ancestors))
            row = conn.execute(
                f"SELECT path, is_folder, config_path, input_space, display, view "
                f"FROM ocio_assignments "
                f"WHERE is_folder = 1 AND path IN ({placeholders}) "
                f"ORDER BY LENGTH(path) DESC LIMIT 1",
                ancestors,
            ).fetchone()
            if row:
                return OcioAssignment(row[0], bool(row[1]), *row[2:])
        except sqlite3.Error as e:
            logger.error("ocio_table.get_for_file failed for %s: %s", file_path, e)
        return None

    def get_all(self) -> List[OcioAssignment]:
        """Return all stored assignments (for management UI)."""
        try:
            conn = self._read_conn()
            rows = conn.execute(
                "SELECT path, is_folder, config_path, input_space, display, view "
                "FROM ocio_assignments ORDER BY path"
            ).fetchall()
            return [OcioAssignment(r[0], bool(r[1]), *r[2:]) for r in rows]
        except sqlite3.Error as e:
            logger.error("ocio_table.get_all failed: %s", e)
            return []

    # close() inherited from BaseTable
