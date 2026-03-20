"""CLIP embedding storage: clip_embeddings table."""

import logging
import sqlite3
import time
from typing import List, Optional

from core.db.base_table import BaseTable

logger = logging.getLogger(__name__)


class EmbeddingTable(BaseTable):
    def __init__(self, db_path: str):
        super().__init__(db_path)
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
            conn = self._read_conn()
            cursor = conn.cursor()
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
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT file_path, embedding FROM clip_embeddings WHERE model_name = ?',
                (model_name,))
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error getting all embeddings: {e}")
            return []

    def get_embeddings_for_files(self, file_paths: List[str],
                                 model_name: str = "clip-vit-b-32") -> List[tuple]:
        """Return (file_path, embedding_blob) pairs for a list of files."""
        if not file_paths:
            return []
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in file_paths)
            query_params = file_paths + [model_name]
            cursor.execute(
                f'''SELECT file_path, embedding
                    FROM clip_embeddings
                    WHERE file_path IN ({placeholders}) AND model_name = ?''',
                query_params
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error getting embeddings for files: {e}")
            return []

    def get_files_missing_embeddings(self, file_paths: List[str],
                                     model_name: str = "clip-vit-b-32") -> List[str]:
        """Return file_paths not yet processed for CLIP embedding.

        A file is done if it has an embedding row OR has been marked in ai_scanned
        (meaning it was attempted but failed — e.g. unsupported format).
        """
        if not file_paths:
            return []
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in file_paths)
            model_type = f'clip:{model_name}'
            cursor.execute(f'''
                SELECT file_path FROM clip_embeddings
                WHERE file_path IN ({placeholders}) AND model_name = ?
                UNION
                SELECT file_path FROM ai_scanned
                WHERE file_path IN ({placeholders}) AND model_type = ?
            ''', file_paths + [model_name] + file_paths + [model_type])
            existing = {row[0] for row in cursor.fetchall()}
            return [fp for fp in file_paths if fp not in existing]
        except sqlite3.Error as e:
            logger.error(f"Error checking missing embeddings: {e}")
            return file_paths  # conservative: assume all missing

    def mark_file_clip_scanned(self, file_path: str,
                               model_name: str = "clip-vit-b-32") -> None:
        """Record that a file was attempted for CLIP embedding (even if it failed)."""
        self.mark_ai_scanned(file_path, f'clip:{model_name}')

    def count_embeddings(self, model_name: str = "clip-vit-b-32") -> int:
        """Return the number of stored embeddings for a model."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
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

    # close() inherited from BaseTable
