"""ImageTable: all image_metadata CRUD operations.

Uses thread-local read connections (WAL) so readers never block on the
write lock.  Write methods serialize through ``_lock`` + ``self.conn``.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.db.base_table import BaseTable
from core.priority import ImageEntry

logger = logging.getLogger(__name__)

_ACCESSED_AT_FLUSH_THRESHOLD = 50


class ImageTable(BaseTable):

    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._accessed_at_buffer: Dict[str, float] = {}
        self._accessed_at_lock = threading.Lock()

    def close(self):
        self._flush_accessed_at()
        super().close()

    # ------------------------------------------------------------------
    #  Utilities
    # ------------------------------------------------------------------

    def _get_metadata_hash(self, file_path: str,
                           stat_result: Optional[os.stat_result] = None) -> Optional[str]:
        """Calculates a fast MD5 hash based on file path, size, and modification time."""
        try:
            stat_info = stat_result or os.stat(file_path)  # disk-io: freshness hash
            info = f"{file_path}-{stat_info.st_size}-{stat_info.st_mtime_ns}"
            return hashlib.md5(info.encode('utf-8')).hexdigest()
        except OSError as e:
            logger.warning(f"Could not stat file {file_path} to generate metadata hash: {e}")
            return None

    @staticmethod
    def _build_entry(file_path: str, sidecars_json: str = "[]") -> ImageEntry:
        """Construct an ImageEntry from DB columns."""
        try:
            sidecars = tuple(json.loads(sidecars_json)) if sidecars_json else ()
        except (json.JSONDecodeError, TypeError):
            sidecars = ()
        return ImageEntry(path=file_path, sidecars=sidecars)

    def _touch_accessed_at(self, file_path: str) -> None:
        """Best-effort LRU timestamp — buffers writes and flushes in batch."""
        now = time.time()
        flush_needed = False
        with self._accessed_at_lock:
            self._accessed_at_buffer[file_path] = now
            if len(self._accessed_at_buffer) >= _ACCESSED_AT_FLUSH_THRESHOLD:
                flush_needed = True
        if flush_needed:
            self._flush_accessed_at()

    def _flush_accessed_at(self) -> None:
        """Write buffered accessed_at timestamps to DB in a single transaction."""
        with self._accessed_at_lock:
            batch = self._accessed_at_buffer.copy()
            self._accessed_at_buffer.clear()
        if not batch:
            return
        try:
            with self._lock:
                self.conn.executemany(
                    'UPDATE image_metadata SET accessed_at = ? WHERE file_path = ?',
                    [(ts, fp) for fp, ts in batch.items()],
                )
                self._soft_commit()
        except sqlite3.Error:
            pass  # why: LRU writes are best-effort; flush failure loses ordering data, not image data

    # ------------------------------------------------------------------
    #  Sidecars
    # ------------------------------------------------------------------

    def update_sidecars(self, file_path: str, sidecars: List[str]) -> None:
        """Store sidecar paths for a file."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE image_metadata SET sidecars = ? WHERE file_path = ?",
                    (json.dumps(sidecars), file_path),
                )
                self._soft_commit()
        except sqlite3.Error as e:
            logger.debug(f"Error updating sidecars for {file_path}: {e}")

    # ------------------------------------------------------------------
    #  Metadata reads
    # ------------------------------------------------------------------

    def get_rating(self, file_path: str) -> int:
        metadata = self.get_metadata(file_path)
        return metadata.get('rating', 0) if metadata else 0

    def get_metadata_batch(self, file_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return metadata for multiple files in a single DB query.

        Skips the per-path ``os.path.exists()`` check so this stays fast on
        network volumes.  Paths not found in the DB map to ``{}``.
        """
        if not file_paths:
            return {}
        results: Dict[str, Dict[str, Any]] = {p: {} for p in file_paths}
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(file_paths))
            cursor.execute(
                f"SELECT * FROM image_metadata WHERE file_path IN ({placeholders})",
                file_paths,
            )
            columns = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                metadata = dict(zip(columns, row))
                if metadata.get("exif_data"):
                    try:
                        metadata["exif_data"] = json.loads(metadata["exif_data"])
                    except json.JSONDecodeError:
                        metadata["exif_data"] = {}
                results[metadata["file_path"]] = metadata
        except sqlite3.Error as e:
            logger.debug(f"Error in get_metadata_batch: {e}")
        return results

    def get_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Gets all metadata for a file strictly from the database.

        Guaranteed to be fast and non-blocking.  May return stale data if the
        file has been modified since the last background scan.
        """
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM image_metadata WHERE file_path = ?', (file_path,))
            result = cursor.fetchone()

            if not result:
                return None

            columns = [desc[0] for desc in cursor.description]
            metadata = dict(zip(columns, result))

            if metadata.get('exif_data'):
                try:
                    metadata['exif_data'] = json.loads(metadata['exif_data'])
                except json.JSONDecodeError:
                    metadata['exif_data'] = {}
            return metadata

        except sqlite3.Error as e:
            logger.debug(f"Error getting metadata for {file_path}: {e}")
            return None

    def needs_full_metadata(self, file_path: str) -> bool:
        """Returns True if the row is missing rich EXIF fields (camera, dimensions, etc.)."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT camera_make, width FROM image_metadata WHERE file_path = ?',
                (file_path,),
            )
            row = cursor.fetchone()
            if row is None:
                return True
            return row[0] is None and (row[1] is None or row[1] == 0)
        except sqlite3.Error:
            return True

    # ------------------------------------------------------------------
    #  Fast metadata (orientation/rating/file_size only)
    # ------------------------------------------------------------------

    def store_fast_metadata(self, file_path: str, fields_dict: Dict[str, Any]) -> None:
        """DB write for fast metadata fields.

        *fields_dict* must contain keys: orientation, rating, file_size, mtime,
        mtime_ns, birthtime.
        """
        orientation = fields_dict['orientation']
        rating = fields_dict['rating']
        file_size = fields_dict['file_size']
        mtime = fields_dict['mtime']
        birthtime = fields_dict['birthtime']
        # Compute hash from fields already available — no os.stat needed.
        info = f"{file_path}-{file_size}-{fields_dict['mtime_ns']}"
        path_hash = hashlib.md5(info.encode('utf-8')).hexdigest()
        current_time = time.time()

        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'SELECT id, rating, orientation, updated_at FROM image_metadata WHERE file_path = ?',
                    (file_path,),
                )
                existing = cursor.fetchone()

                if existing:
                    if existing[3] and existing[3] > mtime:
                        rating = existing[1]
                        orientation = existing[2] or orientation
                    cursor.execute('''
                        UPDATE image_metadata SET
                            path_hash = ?, file_size = ?, orientation = ?,
                            rating = ?, mtime = ?,
                            birthtime = COALESCE(?, birthtime), updated_at = ?
                        WHERE id = ?
                    ''', (path_hash, file_size, orientation, rating, mtime,
                          birthtime, current_time, existing[0]))
                else:
                    cursor.execute('''
                        INSERT INTO image_metadata
                        (file_path, path_hash, file_size, orientation, rating,
                         mtime, birthtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, file_size, orientation, rating,
                          mtime, birthtime, current_time, current_time))
                self._soft_commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            logger.error(f"Error storing fast metadata for {file_path}: {e}")

    # ------------------------------------------------------------------
    #  Full metadata store
    # ------------------------------------------------------------------

    def store_metadata(self, file_path: str, metadata: Dict[str, Any], mtime: float,
                       stat_result: Optional[os.stat_result] = None,
                       birthtime: Optional[float] = None) -> Optional[List[str]]:
        """Stores metadata in the database.

        Returns the ``_keywords`` list (if any) so the caller can insert tags
        via TagTable.  Returns None when there are no keywords.
        """
        try:
            path_hash = self._get_metadata_hash(file_path, stat_result=stat_result)
            current_time = time.time()

            # Serialize EXIF data as JSON
            exif_json = json.dumps(metadata.get('exif_data', {}))

            with self._lock:
                cursor = self.conn.cursor()

                # Check for an existing entry to decide whether to INSERT or UPDATE
                cursor.execute('SELECT id, thumbnail_path, view_image_path, content_hash, rating, updated_at, orientation FROM image_metadata WHERE file_path = ?', (file_path,))
                existing_row = cursor.fetchone()

                # Preserve existing paths to avoid race conditions from other tasks
                if existing_row:
                    if not metadata.get('thumbnail_path'):
                        metadata['thumbnail_path'] = existing_row[1]
                    if not metadata.get('view_image_path'):
                        metadata['view_image_path'] = existing_row[2]
                    if not metadata.get('content_hash'):
                        metadata['content_hash'] = existing_row[3]
                    # Preserve user-set values: if the DB row was updated after
                    # the file was last modified, the value was set explicitly
                    # (e.g. via set_rating/set_orientation) and the EXIF
                    # write-back may not have completed yet.  Don't overwrite
                    # with stale EXIF data.
                    existing_updated_at = existing_row[5]
                    if existing_updated_at and existing_updated_at > mtime:
                        metadata['rating'] = existing_row[4]
                        metadata['orientation'] = existing_row[6] or metadata.get('orientation', 1)

                if existing_row:
                    # UPDATE the existing row
                    cursor.execute('''
                        UPDATE image_metadata SET
                            path_hash = ?, content_hash = ?, file_size = ?, width = ?, height = ?,
                            rating = ?,
                            camera_make = ?, camera_model = ?, lens_model = ?, focal_length = ?, aperture = ?,
                            shutter_speed = ?, iso = ?, date_taken = ?, orientation = ?, color_space = ?,
                            thumbnail_path = ?, view_image_path = ?, exif_data = ?, mtime = ?,
                            birthtime = COALESCE(?, birthtime), updated_at = ?
                        WHERE id = ?
                    ''', (
                        path_hash, metadata.get('content_hash'), metadata.get('file_size', 0), metadata.get('width', 0), metadata.get('height', 0),
                        metadata.get('rating', 0),
                        metadata.get('camera_make'), metadata.get('camera_model'),
                        metadata.get('lens_model'), metadata.get('focal_length'), metadata.get('aperture'),
                        metadata.get('shutter_speed'), metadata.get('iso'), metadata.get('date_taken'),
                        metadata.get('orientation', 1), metadata.get('color_space'), metadata.get('thumbnail_path'),
                        metadata.get('view_image_path'), exif_json, mtime,
                        birthtime, current_time, existing_row[0]
                    ))
                else:
                    # INSERT a new row
                    cursor.execute('''
                        INSERT INTO image_metadata
                        (file_path, path_hash, content_hash, file_size, width, height, rating, camera_make, camera_model, lens_model, focal_length, aperture, shutter_speed, iso, date_taken, orientation, color_space, thumbnail_path, view_image_path, exif_data, mtime, birthtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        file_path, path_hash, metadata.get('content_hash'), metadata.get('file_size', 0), metadata.get('width', 0), metadata.get('height', 0),
                        metadata.get('rating', 0), metadata.get('camera_make'), metadata.get('camera_model'), metadata.get('lens_model'),
                        metadata.get('focal_length'), metadata.get('aperture'), metadata.get('shutter_speed'), metadata.get('iso'),
                        metadata.get('date_taken'), metadata.get('orientation', 1), metadata.get('color_space'),
                        metadata.get('thumbnail_path'), metadata.get('view_image_path'), exif_json, mtime, birthtime, current_time, current_time
                    ))

                self._soft_commit()
                logger.debug(f"Committed full metadata for {file_path}. Rows affected: {cursor.rowcount}")

        except sqlite3.Error as e:
            self.conn.rollback()
            logger.error(f"Error storing metadata for {file_path}: {e}", exc_info=True)

        # Return EXIF keywords so the caller can insert tags via TagTable.
        exif_keywords = metadata.get('_keywords')
        return exif_keywords if exif_keywords else None

    # ------------------------------------------------------------------
    #  Thumbnail paths
    # ------------------------------------------------------------------

    def set_thumbnail_paths(self, file_path: str, thumbnail_path: Optional[str] = None,
                            view_image_path: Optional[str] = None,
                            stat_result: Optional[os.stat_result] = None) -> bool:
        """Sets the thumbnail and view image paths for a file."""
        try:
            current_time = time.time()
            # Stat outside the lock to avoid blocking other DB operations on NAS.
            try:
                st = stat_result or os.stat(file_path)  # disk-io: freshness check
            except OSError:
                st = None

            with self._lock:
                cursor = self.conn.cursor()

                cursor.execute('''
                    SELECT id FROM image_metadata WHERE file_path = ?
                ''', (file_path,))

                if cursor.fetchone():
                    update_fields = []
                    params = []

                    if thumbnail_path is not None:
                        update_fields.append("thumbnail_path = ?")
                        params.append(thumbnail_path)

                    if view_image_path is not None:
                        update_fields.append("view_image_path = ?")
                        params.append(view_image_path)

                    if update_fields:
                        update_fields.append("updated_at = ?")
                        params.append(current_time)
                        params.append(file_path)

                        cursor.execute(f'''
                            UPDATE image_metadata
                            SET {", ".join(update_fields)}
                            WHERE file_path = ?
                        ''', params)
                elif st:
                    path_hash = self._get_metadata_hash(file_path, stat_result=st)
                    cursor.execute('''
                        INSERT INTO image_metadata
                        (file_path, path_hash, file_size, thumbnail_path, view_image_path,
                         mtime, birthtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, st.st_size, thumbnail_path, view_image_path,
                          st.st_mtime, getattr(st, 'st_birthtime', None),
                          current_time, current_time))

                self._soft_commit()
                logger.debug(f"Committed thumbnail paths for {file_path}. Rows affected: {cursor.rowcount}")
                return True

        except sqlite3.Error as e:
            logger.error(f"Error setting thumbnail paths for {file_path}: {e}", exc_info=True)
            return False

    def clear_thumbnail_paths(self, file_path: str) -> bool:
        """Set thumbnail_path and view_image_path to NULL so the file is re-generated."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    UPDATE image_metadata
                    SET thumbnail_path = NULL, view_image_path = NULL, updated_at = ?
                    WHERE file_path = ?
                ''', (time.time(), file_path))
                self._soft_commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error clearing thumbnail paths for {file_path}: {e}")
            return False

    def clear_all_thumbnail_paths(self) -> int:
        """NULL all thumbnail_path and view_image_path columns. Returns rows affected."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    UPDATE image_metadata
                    SET thumbnail_path = NULL, view_image_path = NULL, updated_at = ?
                    WHERE thumbnail_path IS NOT NULL OR view_image_path IS NOT NULL
                ''', (time.time(),))
                self._soft_commit()
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error("Error clearing all thumbnail paths: %s", e)
            return 0

    def clear_thumbnail_paths_for_extensions(self, extensions: List[str]) -> List[Dict[str, Optional[str]]]:
        """Clear thumbnail_path/view_image_path for rows whose file extension matches.

        Returns a list of ``{'file_path', 'thumbnail_path', 'view_image_path'}`` dicts
        for every row that had at least one non-NULL cache path — the caller uses
        these to delete the now-orphaned files from disk.

        ``extensions`` must include the leading dot (e.g. ``['.mp4', '.mov']``).
        Matching is case-insensitive via SQLite ``LOWER()``.
        """
        if not extensions:
            return []
        try:
            patterns = [f"%{ext.lower()}" for ext in extensions]
            where_like = " OR ".join(["LOWER(file_path) LIKE ?"] * len(patterns))

            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    f"""SELECT file_path, thumbnail_path, view_image_path
                        FROM image_metadata
                        WHERE (thumbnail_path IS NOT NULL OR view_image_path IS NOT NULL)
                          AND ({where_like})""",
                    patterns,
                )
                rows = [
                    {'file_path': r[0], 'thumbnail_path': r[1], 'view_image_path': r[2]}
                    for r in cursor.fetchall()
                ]
                if rows:
                    cursor.execute(
                        f"""UPDATE image_metadata
                            SET thumbnail_path = NULL, view_image_path = NULL, updated_at = ?
                            WHERE (thumbnail_path IS NOT NULL OR view_image_path IS NOT NULL)
                              AND ({where_like})""",
                        [time.time(), *patterns],
                    )
                    self._soft_commit()
                return rows
        except sqlite3.Error as e:
            logger.error("Error clearing thumbnail paths for extensions %s: %s", extensions, e)
            return []

    def get_thumbnail_paths(self, file_path: str) -> Dict[str, str]:
        """Gets the thumbnail and view image paths for a file."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT thumbnail_path, view_image_path FROM image_metadata
                WHERE file_path = ?
            ''', (file_path,))

            result = cursor.fetchone()

            if result:
                self._touch_accessed_at(file_path)
                return {
                    'thumbnail_path': result[0],
                    'view_image_path': result[1]
                }

        except sqlite3.Error as e:
            logger.error(f"Error getting thumbnail paths for {file_path}: {e}")

        return {'thumbnail_path': None, 'view_image_path': None}

    def batch_get_thumbnail_validity(self, file_paths: List[str]) -> Dict[str, Dict]:
        """Batch-checks thumbnail validity for multiple files in a single query.

        Returns {path: {thumbnail_path, view_image_path, valid: bool}} for
        paths that have DB records.  Paths without records are omitted.
        """
        if not file_paths:
            return {}

        results: Dict[str, Dict] = {}
        # Stat all files upfront (one syscall each, but outside DB lock).
        stat_cache: Dict[str, os.stat_result] = {}
        for p in file_paths:
            try:
                stat_cache[p] = os.stat(p)  # disk-io: batch stat
            except OSError:
                pass  # file gone -- will be treated as invalid

        try:
            placeholders = ",".join("?" for _ in file_paths)
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT file_path, thumbnail_path, view_image_path, mtime, file_size
                FROM image_metadata
                WHERE file_path IN ({placeholders})
            ''', file_paths)
            rows = cursor.fetchall()

            for fp, thumb, view, stored_mtime, stored_size in rows:
                stat = stat_cache.get(fp)
                valid = (
                    stat is not None
                    and stored_mtime is not None
                    and stored_mtime >= stat.st_mtime
                    and stored_size == stat.st_size
                    and thumb
                    and os.path.exists(thumb)  # disk-io: cache file check
                )
                results[fp] = {
                    'thumbnail_path': thumb,
                    'view_image_path': view,
                    'valid': bool(valid),
                }
        except sqlite3.Error as e:
            logger.error(f"Error in batch_get_thumbnail_validity: {e}")

        return results

    def get_cached_thumbnail_paths(self, file_path: str) -> Optional[Dict[str, str]]:
        """Returns cached thumbnail/view paths without stat-ing the source file.

        Trusts the DB record -- only verifies the local thumbnail file exists.
        Returns None if no cached thumbnail is available.
        """
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT thumbnail_path, view_image_path FROM image_metadata
                WHERE file_path = ?
            ''', (file_path,))
            result = cursor.fetchone()

            if result and result[0] and os.path.exists(result[0]):  # disk-io: cache file check
                self._touch_accessed_at(file_path)
                return {
                    'thumbnail_path': result[0],
                    'view_image_path': result[1],
                }
        except sqlite3.Error as e:
            logger.error(f"Error in get_cached_thumbnail_paths for {file_path}: {e}")
        return None

    def batch_get_cached_thumbnail_validity(self, file_paths: List[str]) -> Dict[str, Dict]:
        """Batch trust-cache check -- no os.stat() on source files.

        Like batch_get_thumbnail_validity but skips mtime/size validation
        against the source file.  Only checks that the DB has a record and
        the local thumbnail file exists.
        """
        if not file_paths:
            return {}

        results: Dict[str, Dict] = {}
        try:
            placeholders = ",".join("?" for _ in file_paths)
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT file_path, thumbnail_path, view_image_path
                FROM image_metadata
                WHERE file_path IN ({placeholders})
            ''', file_paths)
            rows = cursor.fetchall()

            for fp, thumb, view in rows:
                valid = bool(thumb and os.path.exists(thumb))  # disk-io: cache file check
                results[fp] = {
                    'thumbnail_path': thumb,
                    'view_image_path': view,
                    'valid': valid,
                }
        except sqlite3.Error as e:
            logger.error(f"Error in batch_get_cached_thumbnail_validity: {e}")

        return results

    def is_thumbnail_valid(self, file_path: str,
                           stat_result: Optional[os.stat_result] = None) -> bool:
        """Checks if a valid thumbnail exists for the file."""
        valid, _ = self.check_thumbnail_validity(file_path, stat_result=stat_result)
        return valid

    def check_thumbnail_validity(self, file_path: str,
                                  stat_result: Optional[os.stat_result] = None,
                                  ) -> tuple:
        """Combined validity check + path retrieval in a single query.

        Returns ``(is_valid, {'thumbnail_path': ..., 'view_image_path': ...})``.
        """
        paths: Dict[str, Optional[str]] = {'thumbnail_path': None, 'view_image_path': None}
        try:
            stat_info = stat_result or os.stat(file_path)  # disk-io: thumbnail validity
            mtime = stat_info.st_mtime
            file_size = stat_info.st_size

            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT thumbnail_path, view_image_path, mtime, file_size
                FROM image_metadata WHERE file_path = ?
            ''', (file_path,))
            result = cursor.fetchone()

            if result:
                thumb, view, stored_mtime, stored_size = result
                paths = {'thumbnail_path': thumb, 'view_image_path': view}
                if (stored_mtime is not None and stored_mtime >= mtime
                        and stored_size == file_size
                        and thumb
                        and os.path.exists(thumb)):  # disk-io: cache file check
                    return True, paths

        except OSError:  # why: source file missing or NAS I/O error — treat as invalid
            return False, paths
        except sqlite3.Error as e:
            logger.error(f"Error checking thumbnail validity for {file_path}: {e}")

        return False, paths

    # ------------------------------------------------------------------
    #  Rating
    # ------------------------------------------------------------------

    def set_rating(self, file_path: str, rating: int) -> bool:
        """Sets a rating for a file *only* in the database."""
        try:
            current_time = time.time()

            with self._lock:
                cursor = self.conn.cursor()

                cursor.execute('SELECT id FROM image_metadata WHERE file_path = ?', (file_path,))

                if cursor.fetchone():
                    # Update existing entry
                    logger.debug(f"Updating rating for {os.path.basename(file_path)} to {rating} in DB.")
                    cursor.execute('''
                        UPDATE image_metadata
                        SET rating = ?, updated_at = ?
                        WHERE file_path = ?
                    ''', (rating, current_time, file_path))
                else:
                    # Create new entry with minimal metadata
                    logger.debug(f"Inserting new DB entry for {os.path.basename(file_path)} with rating {rating}.")
                    st = os.stat(file_path)  # disk-io: new entry stat
                    path_hash = self._get_metadata_hash(file_path, stat_result=st)
                    file_size = st.st_size
                    mtime = st.st_mtime

                    cursor.execute('''
                        INSERT INTO image_metadata
                        (file_path, path_hash, file_size, rating, mtime, birthtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, file_size, rating, mtime,
                          getattr(st, 'st_birthtime', None), current_time, current_time))

                self.conn.commit()  # immediate: user-initiated rating write
                rowcount = cursor.rowcount

                if rowcount > 0:
                    logger.info(f"Successfully set rating for {os.path.basename(file_path)} to {rating}. Rows affected: {rowcount}.")
                else:
                    logger.warning(f"DB transaction for rating on {os.path.basename(file_path)} completed, but no rows were affected.")
                return True

        except sqlite3.Error as e:
            logger.error(f"Error setting rating for {file_path} in database: {e}", exc_info=True)
            return False

    def batch_set_ratings(self, file_paths: List[str], rating: int) -> tuple:
        """Sets a rating for a batch of files in a single transaction.

        Returns (success: bool, count: int) -- count is the number of files actually written.
        """
        if not file_paths:
            return (True, 0)

        paths_to_process = set(file_paths)
        skipped = 0

        try:
            current_time = time.time()

            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()

                    placeholders = ','.join('?' * len(paths_to_process))
                    cursor.execute(f'SELECT file_path FROM image_metadata WHERE file_path IN ({placeholders})', list(paths_to_process))
                    existing_paths = {row[0] for row in cursor.fetchall()}
                    new_paths = paths_to_process - existing_paths

                    if existing_paths:
                        update_data = [(rating, current_time, path) for path in existing_paths]
                        cursor.executemany('UPDATE image_metadata SET rating = ?, updated_at = ? WHERE file_path = ?', update_data)

                    if new_paths:
                        insert_data = []
                        for path in new_paths:
                            try:
                                stat = os.stat(path)  # disk-io: batch insert stat
                                info = f"{path}-{stat.st_size}-{stat.st_mtime_ns}"
                                path_hash = hashlib.md5(info.encode('utf-8')).hexdigest()
                                insert_data.append((
                                    path, path_hash, stat.st_size, rating, stat.st_mtime,
                                    getattr(stat, 'st_birthtime', None),
                                    current_time, current_time
                                ))
                            except OSError as e:
                                logger.warning(f"Could not stat file for batch insert: {path}, {e}")
                                skipped += 1

                        if insert_data:
                            cursor.executemany('''
                                INSERT INTO image_metadata
                                (file_path, path_hash, file_size, rating, mtime, birthtime, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ''', insert_data)

            written = len(paths_to_process) - skipped
            logger.info(f"Successfully batch-set rating for {written}/{len(file_paths)} files to {rating}.")
            return (skipped == 0, written)
        except sqlite3.Error as e:
            logger.error(f"Error in batch_set_ratings for {len(file_paths)} files: {e}", exc_info=True)
            return (False, 0)

    def get_files_by_rating(self, rating: int) -> List[str]:
        """Gets all files with a specific rating."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT file_path FROM image_metadata
                WHERE rating = ?
                ORDER BY updated_at DESC
            ''', (rating,))
            return [row[0] for row in cursor.fetchall()]

        except sqlite3.Error as e:
            logger.error(f"Error getting files by rating {rating}: {e}")
            return []

    # ------------------------------------------------------------------
    #  Folder Rating
    # ------------------------------------------------------------------

    def get_folder_ratings_batch(self, folder_paths: List[str]) -> Dict[str, int]:
        """Return {folder_path: rating} for a batch of folder paths. Missing → 0."""
        if not folder_paths:
            return {}
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(folder_paths))
            cursor.execute(
                f'SELECT folder_path, rating FROM folder_ratings WHERE folder_path IN ({placeholders})',
                folder_paths,
            )
            result = {row[0]: row[1] for row in cursor.fetchall()}
            for p in folder_paths:
                result.setdefault(p, 0)
            return result
        except sqlite3.Error as e:
            logger.error("Error in get_folder_ratings_batch: %s", e)
            return {p: 0 for p in folder_paths}

    def set_folder_rating(self, folder_path: str, rating: int) -> bool:
        """Store a star rating (0-5) for a folder."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO folder_ratings (folder_path, rating, updated_at) VALUES (?, ?, ?)',
                    (folder_path, max(0, min(5, rating)), time.time()),
                )
                self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Error setting folder rating for %s: %s", folder_path, e)
            return False

    # ------------------------------------------------------------------
    #  Orientation
    # ------------------------------------------------------------------

    def get_orientation(self, file_path: str) -> int:
        """Returns the stored EXIF orientation for a file, or 1 if unknown."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT orientation FROM image_metadata WHERE file_path = ?', (file_path,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else 1
        except sqlite3.Error as e:
            logger.error(f"Error getting orientation for {file_path}: {e}")
            return 1

    def batch_get_orientations(self, file_paths: List[str]) -> Dict[str, int]:
        """Return {file_path: orientation} for a batch of files. Defaults to 1."""
        if not file_paths:
            return {}
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            result = {}
            for i in range(0, len(file_paths), 500):
                batch = file_paths[i:i + 500]
                placeholders = ','.join('?' * len(batch))
                cursor.execute(
                    f'SELECT file_path, orientation FROM image_metadata '
                    f'WHERE file_path IN ({placeholders})', batch)
                for row in cursor.fetchall():
                    result[row[0]] = row[1] if row[1] else 1
            # Fill in missing paths with default
            for fp in file_paths:
                if fp not in result:
                    result[fp] = 1
            return result
        except sqlite3.Error as e:
            logger.error("Error batch getting orientations: %s", e)
            return {fp: 1 for fp in file_paths}

    def set_orientation(self, file_path: str, orientation: int) -> bool:
        """Sets the EXIF orientation for a file in the database."""
        try:
            current_time = time.time()
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    UPDATE image_metadata
                    SET orientation = ?, updated_at = ?
                    WHERE file_path = ?
                ''', (orientation, current_time, file_path))
                self.conn.commit()  # immediate: user-initiated orientation write
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error setting orientation for {file_path}: {e}")
            return False

    # ------------------------------------------------------------------
    #  Search / filter
    # ------------------------------------------------------------------

    def search_by_camera(self, make: Optional[str] = None,
                         model: Optional[str] = None) -> List[str]:
        """Searches for images by camera make and/or model."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            query = "SELECT file_path FROM image_metadata WHERE 1=1"
            params = []
            if make:
                query += " AND camera_make LIKE ?"
                params.append(f"%{make}%")
            if model:
                query += " AND camera_model LIKE ?"
                params.append(f"%{model}%")
            query += " ORDER BY date_taken DESC"
            cursor.execute(query, params)
            return [row[0] for row in cursor.fetchall()]

        except sqlite3.Error as e:
            logger.error(f"Error searching by camera: {e}")
            return []

    def get_date_range_for_paths(self, file_paths: List[str]) -> Optional[Tuple[float, float]]:
        """Returns (min_ts, max_ts) epoch seconds for the given paths using date_taken with mtime fallback.

        Queries in chunks of 500 to stay within SQLite's variable-binding limit.
        Returns None if no paths have a usable date.
        """
        if not file_paths:
            return None
        _CHUNK = 500
        min_ts, max_ts = float('inf'), float('-inf')
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            for i in range(0, len(file_paths), _CHUNK):
                chunk = file_paths[i:i + _CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT MIN(COALESCE(CAST(date_taken AS REAL), mtime)),"
                    f"       MAX(COALESCE(CAST(date_taken AS REAL), mtime))"
                    f"  FROM image_metadata"
                    f" WHERE file_path IN ({placeholders})"
                    f"   AND COALESCE(CAST(date_taken AS REAL), mtime) IS NOT NULL",
                    chunk,
                )
                row = cursor.fetchone()
                if row and row[0] is not None:
                    min_ts = min(min_ts, row[0])
                    max_ts = max(max_ts, row[1])
            if min_ts == float('inf'):
                return None
            return (min_ts, max_ts)
        except sqlite3.Error as e:
            logger.error(f"Error getting date range: {e}")
            return None

    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool],
                                tag_names: Optional[List[str]] = None,
                                duplicates_only: bool = False,
                                date_range: Optional[Tuple[float, float]] = None,
                                directory: Optional[str] = None,
                                recursive: bool = True) -> List[str]:
        """Gets file paths that match the text, star, tag, and duplicates filters."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()

            query = """
                SELECT im.file_path
                FROM image_metadata im
                LEFT JOIN file_transfers ft ON im.file_path = ft.source_path
                    AND ft.operation IN ('delete', 'move') AND ft.status = 'pending'
                WHERE ft.source_path IS NULL
            """
            params: list = []

            # Add directory scope
            if directory is not None:
                search_path = os.path.join(directory, '')
                if recursive:
                    query += " AND im.file_path LIKE ?"
                    params.append(search_path + '%')
                else:
                    query += " AND im.file_path LIKE ? AND SUBSTR(im.file_path, LENGTH(?) + 1) NOT LIKE '%/%'"
                    params.extend([search_path + '%', search_path])

            # Add text filter
            if text_filter:
                query += " AND im.file_path LIKE ?"
                params.append(f"%{text_filter}%")

            # Add star filter
            enabled_ratings = [i for i, state in enumerate(star_states) if state]
            if len(enabled_ratings) < len(star_states) and enabled_ratings:
                placeholders = ", ".join("?" for _ in enabled_ratings)
                query += f" AND im.rating IN ({placeholders})"
                params.extend(enabled_ratings)
            elif not enabled_ratings:
                # If no ratings are selected, match no files
                query += " AND 1=0"

            # Add tag filter
            if tag_names:
                tag_placeholders = ", ".join("?" for _ in tag_names)
                query += f""" AND im.file_path IN (
                    SELECT it.file_path FROM image_tags it
                    JOIN tags t ON t.id = it.tag_id
                    WHERE t.name IN ({tag_placeholders})
                )"""
                params.extend(tag_names)

            # Add duplicates filter
            if duplicates_only:
                query += """ AND im.content_hash IN (
                    SELECT content_hash FROM image_metadata
                    WHERE content_hash IS NOT NULL
                    GROUP BY content_hash HAVING COUNT(*) > 1
                )"""

            # Add date range filter (date_taken with mtime fallback; no-date images excluded)
            if date_range is not None:
                min_ts, max_ts = date_range
                query += """ AND COALESCE(CAST(im.date_taken AS REAL), im.mtime) BETWEEN ? AND ?"""
                params.extend([min_ts, max_ts])

            cursor.execute(query, params)
            return [row[0] for row in cursor.fetchall()]

        except sqlite3.Error as e:
            logger.error(f"Error getting filtered files: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    #  Directory queries
    # ------------------------------------------------------------------

    def get_all_file_paths(self) -> List[str]:
        """Gets a list of all file_path entries from the database."""
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT im.file_path
                FROM image_metadata im
                LEFT JOIN file_transfers ft ON im.file_path = ft.source_path
                    AND ft.operation IN ('delete', 'move') AND ft.status = 'pending'
                WHERE ft.source_path IS NULL
            """)
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting all file paths from database: {e}")
            return []

    def get_directory_files(self, directory_path: str, recursive: bool = False) -> List[str]:
        """Gets file paths from the DB for a directory.

        When *recursive* is True, returns all files under *directory_path*
        (including subdirectories).  When False, returns only direct children.
        """
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            search_path = os.path.join(directory_path, '')
            
            base_query = """
                SELECT im.file_path
                FROM image_metadata im
                LEFT JOIN file_transfers ft ON im.file_path = ft.source_path
                    AND ft.operation IN ('delete', 'move') AND ft.status = 'pending'
                WHERE ft.source_path IS NULL
            """

            if recursive:
                cursor.execute(
                    base_query + " AND im.file_path LIKE ?",
                    (search_path + '%',),
                )
            else:
                cursor.execute(
                    base_query + " AND im.file_path LIKE ? AND SUBSTR(im.file_path, LENGTH(?) + 1) NOT LIKE '%/%'",
                    (search_path + '%', search_path)
                )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Failed to get directory files for {directory_path} from DB: {e}")
            return []

    def get_subdirectory_info(self, parent_path: str) -> List[Dict[str, Any]]:
        """Discover immediate subdirectories with image counts and preview thumbnails.

        Returns a list of dicts with keys: path, name, image_count,
        recursive_count, preview_paths.  All data comes from the existing
        image_metadata table -- no filesystem access.
        """
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            search_path = os.path.join(parent_path, '')

            # Single query: compute per-subdirectory counts and previews.
            # SUBSTR extracts the relative path after parent_path/.
            # INSTR finds the first '/' -- everything before it is the
            # immediate subdirectory name.  Direct-child files (no '/')
            # map to NULL and are excluded.
            cursor.execute("""
                    WITH files AS (
                        SELECT
                            file_path,
                            thumbnail_path,
                            CASE
                                WHEN INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') > 0
                                THEN SUBSTR(file_path, LENGTH(?) + 1,
                                     INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') - 1)
                                ELSE NULL
                            END as subdir,
                            CASE
                                WHEN INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') > 0
                                THEN SUBSTR(file_path,
                                     LENGTH(?) + 1 + INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/'))
                                     NOT LIKE '%/%'
                                ELSE 0
                            END as is_direct
                        FROM image_metadata
                        WHERE file_path LIKE ?
                    )
                    SELECT
                        subdir,
                        COUNT(*) as recursive_count,
                        SUM(is_direct) as direct_count
                    FROM files
                    WHERE subdir IS NOT NULL
                    GROUP BY subdir
                    ORDER BY subdir
                """, (search_path, search_path, search_path,
                      search_path, search_path, search_path,
                      search_path + '%'))
            subdir_rows = cursor.fetchall()

            if not subdir_rows:
                return []

            # Fetch up to 4 preview thumbnails per subdirectory in one query
            placeholders = ','.join('?' * len(subdir_rows))
            subdir_names = [row[0] for row in subdir_rows]
            preview_map: Dict[str, List[str]] = {name: [] for name in subdir_names}

            cursor.execute(f"""
                SELECT subdir, thumbnail_path FROM (
                    SELECT
                        SUBSTR(file_path, LENGTH(?) + 1,
                               INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') - 1) as subdir,
                        thumbnail_path,
                        ROW_NUMBER() OVER (
                            PARTITION BY SUBSTR(file_path, LENGTH(?) + 1,
                                INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') - 1)
                        ) as rn
                    FROM image_metadata
                    WHERE file_path LIKE ?
                      AND thumbnail_path IS NOT NULL
                      AND thumbnail_path != ''
                      AND INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') > 0
                      AND SUBSTR(file_path, LENGTH(?) + 1,
                          INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') - 1) IN ({placeholders})
                )
                WHERE rn <= 4
            """, (search_path, search_path, search_path, search_path,
                  search_path + '%', search_path, search_path, search_path,
                  *subdir_names))

            for subdir_name, thumb_path in cursor.fetchall():
                if subdir_name in preview_map:
                    preview_map[subdir_name].append(thumb_path)

            results = []
            for subdir_name, recursive_count, direct_count in subdir_rows:
                results.append({
                    'path': os.path.join(parent_path, subdir_name),
                    'name': subdir_name,
                    'image_count': direct_count,
                    'recursive_count': recursive_count,
                    'preview_paths': preview_map.get(subdir_name, []),
                })

            return results
        except sqlite3.Error as e:
            logger.error(f"Failed to get subdirectory info for {parent_path}: {e}")
            return []

    # ------------------------------------------------------------------
    #  Batch / bulk operations
    # ------------------------------------------------------------------

    def batch_ensure_records_exist(self, file_paths: List[str]) -> None:
        """Creates minimal DB records for files that don't already exist.

        Uses INSERT OR IGNORE so a concurrent insert between the existence
        check and the bulk insert is harmless (no stale-state risk).
        """
        if not file_paths:
            return

        current_time = time.time()

        conn = self._read_conn()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(file_paths))
        cursor.execute(f'SELECT file_path FROM image_metadata WHERE file_path IN ({placeholders})', file_paths)
        existing_paths = {row[0] for row in cursor.fetchall()}

        new_paths = [p for p in file_paths if p not in existing_paths]
        if not new_paths:
            return

        # Stat outside the lock to avoid blocking DB on NAS round-trips
        logger.info(f"Batch inserting {len(new_paths)} new minimal records into database.")
        records_to_insert = []
        for path in new_paths:
            try:
                st = os.stat(path)  # disk-io: batch insert stat
                path_hash = self._get_metadata_hash(path, stat_result=st)
                records_to_insert.append((
                    path, path_hash, st.st_size, st.st_mtime,
                    getattr(st, 'st_birthtime', None),
                    current_time, current_time
                ))
            except OSError:
                continue  # why: file may have been deleted between scan and stat

        if records_to_insert:
            with self._lock:
                with self.conn:
                    self.conn.executemany("""
                        INSERT OR IGNORE INTO image_metadata (file_path, path_hash, file_size, mtime, birthtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, records_to_insert)

    def delete(self, file_paths: List[str]) -> tuple:
        """Delete image_metadata rows for the given file paths.

        Returns (rows_affected, cache_paths_to_delete) where cache_paths_to_delete
        is a list of (thumbnail_path, view_image_path) tuples.  Does NOT touch
        auxiliary tables or the filesystem.
        """
        if not file_paths:
            return (0, [])

        try:
            with self._lock:
                placeholders = ','.join('?' for _ in file_paths)
                cursor = self.conn.cursor()
                cursor.execute(f'''
                    SELECT thumbnail_path, view_image_path FROM image_metadata
                    WHERE file_path IN ({placeholders})
                ''', file_paths)
                cache_paths_to_delete = cursor.fetchall()

                cursor.execute(f'''
                    DELETE FROM image_metadata WHERE file_path IN ({placeholders})
                ''', file_paths)
                rows_affected = cursor.rowcount
                self.conn.commit()  # immediate: destructive delete

            logger.info(f"Deleted {rows_affected} records from database for {len(file_paths)} files.")
            return (rows_affected, cache_paths_to_delete)

        except sqlite3.Error as e:
            logger.error(f"Error deleting records for {len(file_paths)} files: {e}", exc_info=True)
            return (0, [])

    def cleanup_missing_files(self) -> None:
        """Removes entries for files that no longer exist."""
        try:
            # Fetch all paths via read connection, then do filesystem I/O outside any lock.
            conn = self._read_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT file_path FROM image_metadata')
            all_paths = [row[0] for row in cursor.fetchall()]

            # Filesystem existence checks happen outside the lock to avoid blocking DB operations.
            missing_paths = [p for p in all_paths if not os.path.exists(p)]  # disk-io: ghost cleanup

            if missing_paths:
                with self._lock:
                    cursor = self.conn.cursor()
                    cursor.executemany(
                        'DELETE FROM image_metadata WHERE file_path = ?',
                        [(path,) for path in missing_paths]
                    )
                    self.conn.commit()  # immediate: destructive cleanup
                logger.info(f"Cleaned up {len(missing_paths)} missing files from metadata database")

        except sqlite3.Error as e:
            logger.error(f"Error cleaning up metadata database: {e}")

    # ------------------------------------------------------------------
    #  Content hash
    # ------------------------------------------------------------------

    def set_content_hash(self, file_path: str, content_hash: str) -> bool:
        """Sets the full content hash for a file that already has an entry."""
        if not content_hash:
            return False

        try:
            with self._lock:
                cursor = self.conn.cursor()

                cursor.execute('''
                    UPDATE image_metadata
                    SET content_hash = ?, updated_at = ?
                    WHERE file_path = ?
                ''', (content_hash, time.time(), file_path))

                self._soft_commit()
                if cursor.rowcount > 0:
                    logger.debug(f"Set content_hash for {os.path.basename(file_path)}")
                else:
                    logger.warning(f"Could not set content_hash for {os.path.basename(file_path)}, file path not found in DB.")
                return True
        except sqlite3.Error as e:
            logger.error(f"Error setting content hash for {file_path}: {e}")
            return False

    # ------------------------------------------------------------------
    #  Perceptual hash
    # ------------------------------------------------------------------

    def set_phash(self, file_path: str, phash_int: int) -> bool:
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE image_metadata SET phash = ?, updated_at = ? WHERE file_path = ?",
                    (phash_int, time.time(), file_path),
                )
                self._soft_commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Error setting phash for %s: %s", os.path.basename(file_path), e)
            return False

    def get_files_missing_phash(self, file_paths: List[str]) -> List[str]:
        """Returns the subset of file_paths that have a thumbnail but no phash."""
        if not file_paths:
            return []
        results = []
        chunk_size = 900
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            for i in range(0, len(file_paths), chunk_size):
                chunk = file_paths[i:i + chunk_size]
                placeholders = ','.join('?' for _ in chunk)
                cursor.execute(
                    f"SELECT file_path FROM image_metadata "
                    f"WHERE file_path IN ({placeholders}) "
                    f"AND thumbnail_path IS NOT NULL AND phash IS NULL",
                    chunk,
                )
                results.extend(row[0] for row in cursor.fetchall())
            return results
        except sqlite3.Error as e:
            logger.error("Error getting files missing phash: %s", e)
            return []

    def get_phash_pairs(self, file_paths: List[str]) -> List[Tuple[str, int]]:
        """Returns (file_path, phash) for files in file_paths that have a phash."""
        if not file_paths:
            return []
        results = []
        chunk_size = 900
        try:
            conn = self._read_conn()
            cursor = conn.cursor()
            for i in range(0, len(file_paths), chunk_size):
                chunk = file_paths[i:i + chunk_size]
                placeholders = ','.join('?' for _ in chunk)
                cursor.execute(
                    f"SELECT file_path, phash FROM image_metadata "
                    f"WHERE file_path IN ({placeholders}) AND phash IS NOT NULL",
                    chunk,
                )
                results.extend((row[0], row[1]) for row in cursor.fetchall())
            return results
        except sqlite3.Error as e:
            logger.error("Error getting phash pairs: %s", e)
            return []

    # ------------------------------------------------------------------
    #  Move / rename
    # ------------------------------------------------------------------

    def move_records(self, moves: List[Dict[str, str]]) -> int:
        """Atomically renames file_path entries for moved files.

        Args:
            moves: List of {"old_path": ..., "new_path": ...} dicts.

        Returns:
            Number of rows updated.
        """
        if not moves:
            return 0

        current_time = time.time()
        updated = 0
        try:
            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()
                    for move in moves:
                        cursor.execute(
                            'UPDATE image_metadata SET file_path = ?, updated_at = ? WHERE file_path = ?',
                            (move["new_path"], current_time, move["old_path"]),
                        )
                        updated += cursor.rowcount
            logger.info(f"move_records: updated {updated}/{len(moves)} rows.")
        except sqlite3.Error as e:
            logger.error(f"Error in move_records: {e}", exc_info=True)
        return updated

    # ------------------------------------------------------------------
    #  Daemon helpers
    # ------------------------------------------------------------------

    def get_files_missing_thumbnails(self, watch_paths: list) -> List[str]:
        """Return file paths under *watch_paths* that have a DB record but no thumbnail."""
        if not watch_paths:
            return []
        try:
            clauses = " OR ".join(["file_path LIKE ?"] * len(watch_paths))
            params = [p.rstrip("/") + "/%" for p in watch_paths]
            conn = self._read_conn()
            cursor = conn.execute(
                f"SELECT file_path FROM image_metadata "
                f"WHERE thumbnail_path IS NULL AND ({clauses})",
                params,
            )
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("get_files_missing_thumbnails failed: %s", e)
            return []

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    # close() inherited from BaseTable
