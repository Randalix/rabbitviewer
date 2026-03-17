"""Face recognition storage: face_detections and persons tables."""

import logging
import sqlite3
import time
import uuid
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional

from core.db.connection import create_connection

logger = logging.getLogger(__name__)


class FaceTable:
    def __init__(self, db_path: str):
        self.conn = create_connection(db_path)
        self._lock = Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    # ------------------------------------------------------------------
    #  Face detections
    # ------------------------------------------------------------------

    def insert_face_detection(self, face_id: str, file_path: str, embedding: bytes,
                              bbox: tuple, confidence: float, model_name: str,
                              person_id: str = None):
        """Insert a detected face. bbox is (x, y, w, h) normalized 0-1."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO face_detections
                    (face_id, file_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h,
                     confidence, model_name, person_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (face_id, file_path, embedding, bbox[0], bbox[1], bbox[2], bbox[3],
                      confidence, model_name, person_id, time.time()))
                self.conn.commit()
                self._generation += 1
        except sqlite3.Error as e:
            logger.error(f"Error inserting face detection: {e}")

    def get_faces_for_file(self, file_path: str) -> List[dict]:
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT face_id, person_id, embedding, bbox_x, bbox_y, bbox_w, bbox_h,
                           confidence, model_name
                    FROM face_detections WHERE file_path = ?
                ''', (file_path,))
                rows = cursor.fetchall()
            return [{'face_id': r[0], 'person_id': r[1], 'embedding': r[2],
                     'bbox': (r[3], r[4], r[5], r[6]), 'confidence': r[7],
                     'model_name': r[8]} for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting faces for file: {e}")
            return []

    def get_faces_for_person(self, person_id: str) -> List[dict]:
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT face_id, file_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h,
                           confidence
                    FROM face_detections WHERE person_id = ?
                ''', (person_id,))
                rows = cursor.fetchall()
            return [{'face_id': r[0], 'file_path': r[1], 'embedding': r[2],
                     'bbox': (r[3], r[4], r[5], r[6]), 'confidence': r[7]}
                    for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting faces for person: {e}")
            return []

    def get_all_person_embeddings(self) -> List[dict]:
        """Return all face embeddings that are assigned to a person.

        Single query returning [{person_id, embedding}, ...].
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT person_id, embedding
                    FROM face_detections WHERE person_id IS NOT NULL
                ''')
                rows = cursor.fetchall()
            return [{'person_id': r[0], 'embedding': r[1]} for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting all person embeddings: {e}")
            return []

    def get_feature_faces_batch(self, person_feature_map: Dict[str, Optional[str]]) -> Dict[str, dict]:
        """Return {person_id: face_dict} for each person's feature face (or first face).

        person_feature_map: {person_id: feature_face_id_or_None}
        Single query fetches one face per person — preferring the feature face.
        """
        if not person_feature_map:
            return {}
        try:
            with self._lock:
                cursor = self.conn.cursor()
                pids = list(person_feature_map.keys())
                placeholders = ','.join('?' * len(pids))
                cursor.execute(f'''
                    SELECT face_id, file_path, person_id,
                           bbox_x, bbox_y, bbox_w, bbox_h
                    FROM face_detections WHERE person_id IN ({placeholders})
                ''', pids)
                rows = cursor.fetchall()

            # Group by person_id, pick feature face or first
            by_person = defaultdict(list)
            for r in rows:
                by_person[r[2]].append({
                    'face_id': r[0], 'file_path': r[1],
                    'bbox': (r[3], r[4], r[5], r[6]),
                })

            result = {}
            for pid, faces in by_person.items():
                feature_fid = person_feature_map.get(pid)
                chosen = None
                if feature_fid:
                    for f in faces:
                        if f['face_id'] == feature_fid:
                            chosen = f
                            break
                result[pid] = chosen or faces[0]
            return result
        except sqlite3.Error as e:
            logger.error(f"Error batch-fetching feature faces: {e}")
            return {}

    def get_all_face_embeddings(self) -> List[tuple]:
        """Return all (face_id, embedding_blob, person_id) tuples."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('SELECT face_id, embedding, person_id FROM face_detections')
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Error getting all face embeddings: {e}")
            return []

    def get_files_missing_faces(self, file_paths: List[str],
                                model_name: str = "buffalo_l") -> List[str]:
        """Return file_paths that have no face detection for the given model."""
        if not file_paths:
            return []
        try:
            with self._lock:
                cursor = self.conn.cursor()
                placeholders = ','.join('?' for _ in file_paths)
                cursor.execute(f'''
                    SELECT DISTINCT file_path FROM face_detections
                    WHERE file_path IN ({placeholders}) AND model_name = ?
                ''', file_paths + [model_name])
                existing = {row[0] for row in cursor.fetchall()}
            return [fp for fp in file_paths if fp not in existing]
        except sqlite3.Error as e:
            logger.error(f"Error checking missing faces: {e}")
            return file_paths

    # ------------------------------------------------------------------
    #  Person management
    # ------------------------------------------------------------------

    def assign_face_to_person(self, face_id: str, person_id: str):
        """Assign a face to a person and update face_count."""
        try:
            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()
                    cursor.execute('SELECT person_id FROM face_detections WHERE face_id = ?', (face_id,))
                    row = cursor.fetchone()
                    if not row:
                        return
                    old_person_id = row[0]

                    cursor.execute('UPDATE face_detections SET person_id = ? WHERE face_id = ?',
                                   (person_id, face_id))

                    if old_person_id and old_person_id != person_id:
                        cursor.execute('''
                            UPDATE persons SET face_count = MAX(0, face_count - 1), updated_at = ?
                            WHERE person_id = ?
                        ''', (time.time(), old_person_id))

                    cursor.execute('''
                        UPDATE persons SET face_count = face_count + 1, updated_at = ?
                        WHERE person_id = ?
                    ''', (time.time(), person_id))
                self._generation += 1
        except sqlite3.Error as e:
            logger.error(f"Error assigning face to person: {e}")

    def create_person(self, person_id: str, name: str = '',
                      feature_face_id: str = None) -> bool:
        try:
            now = time.time()
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO persons
                    (person_id, name, feature_face_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (person_id, name, feature_face_id, now, now))
                self.conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error creating person: {e}")
            return False

    def get_all_persons(self, include_hidden: bool = False) -> List[dict]:
        try:
            with self._lock:
                cursor = self.conn.cursor()
                if include_hidden:
                    cursor.execute('SELECT person_id, name, face_count, feature_face_id, is_hidden FROM persons')
                else:
                    cursor.execute('SELECT person_id, name, face_count, feature_face_id, is_hidden FROM persons WHERE is_hidden = 0')
                rows = cursor.fetchall()
            return [{'person_id': r[0], 'name': r[1], 'face_count': r[2],
                     'feature_face_id': r[3], 'is_hidden': bool(r[4])} for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting all persons: {e}")
            return []

    def get_persons_for_files(self, file_paths: List[str],
                              include_hidden: bool = False) -> List[dict]:
        """Return persons that have at least one face in the given file_paths."""
        if not file_paths:
            return []
        try:
            with self._lock:
                cursor = self.conn.cursor()
                placeholders = ','.join('?' * len(file_paths))
                hidden_clause = "" if include_hidden else " AND p.is_hidden = 0"
                cursor.execute(f'''
                    SELECT p.person_id, p.name, p.face_count, p.feature_face_id, p.is_hidden,
                           COUNT(f.face_id) AS visible_face_count
                    FROM persons p
                    INNER JOIN face_detections f ON f.person_id = p.person_id
                    WHERE f.file_path IN ({placeholders}){hidden_clause}
                    GROUP BY p.person_id
                ''', file_paths)
                rows = cursor.fetchall()
            return [{'person_id': r[0], 'name': r[1], 'face_count': r[2],
                     'feature_face_id': r[3], 'is_hidden': bool(r[4]),
                     'visible_face_count': r[5]} for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting persons for files: {e}")
            return []

    def rename_person(self, person_id: str, name: str):
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('UPDATE persons SET name = ?, updated_at = ? WHERE person_id = ?',
                               (name, time.time(), person_id))
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error renaming person: {e}")

    def merge_persons(self, target_id: str, source_ids: List[str]):
        """Reassign all faces from source persons to target, delete sources."""
        try:
            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()
                    for src_id in source_ids:
                        cursor.execute('UPDATE face_detections SET person_id = ? WHERE person_id = ?',
                                       (target_id, src_id))
                        cursor.execute('DELETE FROM persons WHERE person_id = ?', (src_id,))
                    cursor.execute('''
                        UPDATE persons SET face_count = (
                            SELECT COUNT(*) FROM face_detections WHERE person_id = ?
                        ), updated_at = ? WHERE person_id = ?
                    ''', (target_id, time.time(), target_id))
                self._generation += 1
        except sqlite3.Error as e:
            logger.error(f"Error merging persons: {e}")

    def ungroup_person(self, person_id: str):
        """Split a person into individual persons — one per face."""
        try:
            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()
                    cursor.execute(
                        'SELECT face_id FROM face_detections WHERE person_id = ?',
                        (person_id,))
                    face_ids = [r[0] for r in cursor.fetchall()]
                    if len(face_ids) < 2:
                        return
                    now = time.time()
                    for fid in face_ids:
                        new_pid = str(uuid.uuid4())
                        cursor.execute('''
                            INSERT INTO persons
                            (person_id, name, face_count, feature_face_id, created_at, updated_at)
                            VALUES (?, '', 1, ?, ?, ?)
                        ''', (new_pid, fid, now, now))
                        cursor.execute(
                            'UPDATE face_detections SET person_id = ? WHERE face_id = ?',
                            (new_pid, fid))
                    cursor.execute('DELETE FROM persons WHERE person_id = ?',
                                   (person_id,))
                self._generation += 1
        except sqlite3.Error as e:
            logger.error(f"Error ungrouping person: {e}")

    def hide_person(self, person_id: str, hidden: bool):
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('UPDATE persons SET is_hidden = ?, updated_at = ? WHERE person_id = ?',
                               (int(hidden), time.time(), person_id))
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error hiding person: {e}")

    def set_feature_face(self, person_id: str, face_id: str):
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('UPDATE persons SET feature_face_id = ?, updated_at = ? WHERE person_id = ?',
                               (face_id, time.time(), person_id))
                self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error setting feature face: {e}")

    def get_face_paths_for_person(self, person_id: str) -> List[str]:
        """Return distinct file_paths for a person (for filter)."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT DISTINCT file_path FROM face_detections WHERE person_id = ?
                ''', (person_id,))
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting face paths for person: {e}")
            return []

    def get_face_paths_for_persons(self, person_ids: List[str]) -> List[str]:
        """Return distinct file_paths for multiple persons (union, for multi-person filter)."""
        if not person_ids:
            return []
        try:
            with self._lock:
                cursor = self.conn.cursor()
                placeholders = ','.join('?' * len(person_ids))
                cursor.execute(f'''
                    SELECT DISTINCT file_path FROM face_detections
                    WHERE person_id IN ({placeholders})
                ''', person_ids)
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting face paths for persons: {e}")
            return []

    def get_person_names(self) -> List[str]:
        """Return all non-empty person names (for autocomplete)."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute("SELECT DISTINCT name FROM persons WHERE name != '' ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting person names: {e}")
            return []

    def get_person_by_name(self, name: str) -> Optional[dict]:
        """Return person dict for exact name match, or None."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('SELECT person_id, name, face_count, feature_face_id FROM persons WHERE name = ?', (name,))
                row = cursor.fetchone()
                if row:
                    return {'person_id': row[0], 'name': row[1], 'face_count': row[2], 'feature_face_id': row[3]}
                return None
        except sqlite3.Error as e:
            logger.error(f"Error getting person by name: {e}")
            return None

    def delete_for_files(self, file_paths: List[str]) -> int:
        """Delete face detections for the given file paths."""
        if not file_paths:
            return 0
        try:
            placeholders = ','.join('?' for _ in file_paths)
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    f'DELETE FROM face_detections WHERE file_path IN ({placeholders})',
                    file_paths,
                )
                self.conn.commit()
                rowcount = cursor.rowcount
                if rowcount:
                    self._generation += 1
                return rowcount
        except sqlite3.Error as e:
            logger.error(f"face delete_for_files failed: {e}")
            return 0

    def close(self):
        with self._lock:
            self.conn.close()
