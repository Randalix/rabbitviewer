"""Tag CRUD: tags + image_tags junction table."""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from core.db.base_table import BaseTable

logger = logging.getLogger(__name__)


class TagTable(BaseTable):

    def _get_or_create_tag(self, name: str, kind: str = 'keyword') -> int:
        """Returns the tag id, creating the row if needed. Caller must hold _lock."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM tags WHERE name = ?', (name,))
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute('INSERT INTO tags (name, kind) VALUES (?, ?)', (name, kind))
        return cursor.lastrowid

    def get_or_create_tag(self, name: str, kind: str = 'keyword') -> int:
        """Public version that acquires the lock."""
        with self._lock:
            tag_id = self._get_or_create_tag(name, kind)
            self.conn.commit()
            return tag_id

    def add_image_tags(self, file_path: str, tag_names: List[str]) -> None:
        """Adds tags to an image without removing existing ones."""
        if not tag_names:
            return
        try:
            with self._lock:
                with self.conn:
                    for name in tag_names:
                        tag_id = self._get_or_create_tag(name)
                        self.conn.execute(
                            'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                            (file_path, tag_id),
                        )
        except sqlite3.Error as e:
            logger.error(f"Error adding tags for {file_path}: {e}")

    def remove_image_tags(self, file_path: str, tag_names: List[str]) -> None:
        """Removes specific tags from an image."""
        if not tag_names:
            return
        try:
            with self._lock:
                with self.conn:
                    placeholders = ','.join('?' for _ in tag_names)
                    self.conn.execute(f'''
                        DELETE FROM image_tags
                        WHERE file_path = ?
                          AND tag_id IN (SELECT id FROM tags WHERE name IN ({placeholders}))
                    ''', [file_path] + list(tag_names))
        except sqlite3.Error as e:
            logger.error(f"Error removing tags for {file_path}: {e}")

    def set_image_tags(self, file_path: str, tag_names: List[str]) -> None:
        """Replaces all tags for an image with the given list."""
        try:
            with self._lock:
                with self.conn:
                    self.conn.execute('DELETE FROM image_tags WHERE file_path = ?', (file_path,))
                    for name in tag_names:
                        tag_id = self._get_or_create_tag(name)
                        self.conn.execute(
                            'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                            (file_path, tag_id),
                        )
        except sqlite3.Error as e:
            logger.error(f"Error setting tags for {file_path}: {e}")

    def batch_set_tags(self, file_paths: List[str], tag_names: List[str]) -> bool:
        """Adds tags to multiple images in a single transaction."""
        if not file_paths or not tag_names:
            return False
        try:
            with self._lock:
                with self.conn:
                    tag_ids = [self._get_or_create_tag(name) for name in tag_names]
                    for fp in file_paths:
                        for tid in tag_ids:
                            self.conn.execute(
                                'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                                (fp, tid),
                            )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error in batch_set_tags: {e}")
            return False

    def batch_remove_tags(self, file_paths: List[str], tag_names: List[str]) -> bool:
        """Removes specific tags from multiple images in a single transaction."""
        if not file_paths or not tag_names:
            return False
        try:
            with self._lock:
                with self.conn:
                    tag_placeholders = ','.join('?' for _ in tag_names)
                    file_placeholders = ','.join('?' for _ in file_paths)
                    self.conn.execute(f'''
                        DELETE FROM image_tags
                        WHERE file_path IN ({file_placeholders})
                          AND tag_id IN (SELECT id FROM tags WHERE name IN ({tag_placeholders}))
                    ''', list(file_paths) + list(tag_names))
            return True
        except sqlite3.Error as e:
            logger.error(f"Error in batch_remove_tags: {e}")
            return False

    def get_image_tags(self, file_path: str) -> List[str]:
        """Returns tag names for a single image."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT t.name FROM tags t
                JOIN image_tags it ON it.tag_id = t.id
                WHERE it.file_path = ?
                ORDER BY t.name
            ''', (file_path,))
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting tags for {file_path}: {e}")
            return []

    def batch_get_image_tags(self, file_paths: List[str]) -> Dict[str, List[str]]:
        """Returns {file_path: [tag_names]} for multiple images in a single query."""
        if not file_paths:
            return {}
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in file_paths)
            cursor.execute(f'''
                SELECT it.file_path, t.name FROM tags t
                JOIN image_tags it ON it.tag_id = t.id
                WHERE it.file_path IN ({placeholders})
                ORDER BY it.file_path, t.name
            ''', file_paths)
            result: Dict[str, List[str]] = {fp: [] for fp in file_paths}
            for file_path, tag_name in cursor.fetchall():
                result[file_path].append(tag_name)
            return result
        except sqlite3.Error as e:
            logger.error(f"Error batch getting tags: {e}")
            return {fp: [] for fp in file_paths}

    def get_all_tags(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all tags as [{id, name, kind}], optionally filtered by kind."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            if kind:
                cursor.execute('SELECT id, name, kind FROM tags WHERE kind = ? ORDER BY name', (kind,))
            else:
                cursor.execute('SELECT id, name, kind FROM tags ORDER BY name')
            return [{'id': r[0], 'name': r[1], 'kind': r[2]} for r in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting all tags: {e}")
            return []

    def get_directory_tags(self, directory_path: str) -> List[Dict[str, Any]]:
        """Returns tags used by images under a directory."""
        try:
            search_path = os.path.join(directory_path, '')
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT t.id, t.name, t.kind FROM tags t
                JOIN image_tags it ON it.tag_id = t.id
                WHERE it.file_path LIKE ?
                ORDER BY t.name
            ''', (search_path + '%',))
            return [{'id': r[0], 'name': r[1], 'kind': r[2]} for r in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting directory tags for {directory_path}: {e}")
            return []

    # close() inherited from BaseTable
