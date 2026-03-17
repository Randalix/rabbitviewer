"""CLIP embedding storage: clip_embeddings table."""

import logging
import sqlite3
import time
from threading import Lock
from typing import List, Optional

from core.db.connection import create_connection

logger = logging.getLogger(__name__)


class EmbeddingTable:
    def __init__(self, db_path: str):
        self.conn = create_connection(db_path)
        self._lock = Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def upsert_embedding(self, file_path: str, embedding: bytes,
                         model_name: str = "clip-vit-b-32") -> bool:
        """Store or update a CLIP embedding for a file."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    INSERT INTO clip_embeddings (file_path, embedding, model_name, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        embedding = excluded.embedding,
                        model_name = excluded.model_name,
                        created_at = excluded.created_at
                ''', (file_path, embedding, model_name, time.time()))
                self.conn.commit()
                self._generation += 1
                return True
        except sqlite3.Error as e:
            logger.error(f"Error upserting embedding for {file_path}: {e}")
            return False

    def get_embedding(self, file_path: str) -> Optional[bytes]:
        """Return the raw embedding BLOB for a file, or None."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'SELECT embedding FROM clip_embeddings WHERE file_path = ?',
                    (file_path,))
                row = cursor.fetchone()
                return row[0] if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting embedding for {file_path}: {e}")
            return None

    def get_all_embeddings(self, model_name: str = "clip-vit-b-32") -> List[tuple]:
        """Return all (file_path, embedding_blob) pairs for a model."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'SELECT file_path, embedding FROM clip_embeddings WHERE model_name = ?',
                    (model_name,))
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error getting all embeddings: {e}")
            return []

    def get_files_missing_embeddings(self, file_paths: List[str],
                                     model_name: str = "clip-vit-b-32") -> List[str]:
        """Return file_paths that have no embedding for the given model."""
        if not file_paths:
            return []
        try:
            with self._lock:
                cursor = self.conn.cursor()
                placeholders = ','.join('?' for _ in file_paths)
                cursor.execute(f'''
                    SELECT file_path FROM clip_embeddings
                    WHERE file_path IN ({placeholders}) AND model_name = ?
                ''', file_paths + [model_name])
                existing = {row[0] for row in cursor.fetchall()}
            return [fp for fp in file_paths if fp not in existing]
        except sqlite3.Error as e:
            logger.error(f"Error checking missing embeddings: {e}")
            return file_paths  # conservative: assume all missing

    def count_embeddings(self, model_name: str = "clip-vit-b-32") -> int:
        """Return the number of stored embeddings for a model."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'SELECT COUNT(*) FROM clip_embeddings WHERE model_name = ?',
                    (model_name,))
                return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error counting embeddings: {e}")
            return 0

    def delete_for_files(self, file_paths: List[str]) -> int:
        """Delete embeddings for the given file paths."""
        if not file_paths:
            return 0
        try:
            placeholders = ','.join('?' for _ in file_paths)
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    f'DELETE FROM clip_embeddings WHERE file_path IN ({placeholders})',
                    file_paths,
                )
                self.conn.commit()
                rowcount = cursor.rowcount
                if rowcount:
                    self._generation += 1
                return rowcount
        except sqlite3.Error as e:
            logger.error(f"embedding delete_for_files failed: {e}")
            return 0

    def close(self):
        with self._lock:
            self.conn.close()
