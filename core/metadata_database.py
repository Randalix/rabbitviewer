import sqlite3
import os
import logging
import hashlib
from typing import Dict, Optional, List, Any
import threading
from threading import Lock
import time
import json
from plugins.base_plugin import plugin_registry
from plugins.exiftool_process import ExifToolProcess
from core.priority import ImageEntry

_fallback_exiftool_local = threading.local()


def _get_fallback_exiftool() -> ExifToolProcess:
    if not hasattr(_fallback_exiftool_local, "proc"):
        _fallback_exiftool_local.proc = ExifToolProcess()
    return _fallback_exiftool_local.proc


class MetadataDatabase:
    """
    Unified database for all image metadata (rating, EXIF, file size, etc.).
    """
    
    def __init__(self, db_path: str):
        logging.info(f"Initializing MetadataDatabase with path: {db_path}")
        self.db_path = db_path
        self._lock = Lock()
        
        # Ensure database directory exists
        db_dir = os.path.dirname(db_path)
        if db_dir: # Only create directory if db_dir is not an empty string
            os.makedirs(db_dir, exist_ok=True)
        
        # Initialize database connection with check_same_thread=False for multi-threading
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        
        # Initialize database
        self._init_database()
        
    def _init_database(self):
        with self._lock:
            try:
                cursor = self.conn.cursor()
                
                # Enable Write-Ahead Logging for better concurrency
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                
                # Create metadata table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS image_metadata (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT UNIQUE NOT NULL,
                        path_hash TEXT NOT NULL, -- Fast hash based on path, size, and mtime.
                        content_hash TEXT, -- Full content hash (e.g., MD5), populated as a low-priority background task.
                        file_size INTEGER,
                        width INTEGER,
                        height INTEGER,
                        rating INTEGER DEFAULT 0,
                        camera_make TEXT,
                        camera_model TEXT,
                        lens_model TEXT,
                        focal_length REAL,
                        aperture REAL,
                        shutter_speed TEXT,
                        iso INTEGER,
                        date_taken TEXT,
                        orientation INTEGER,
                        color_space TEXT,
                        thumbnail_path TEXT,  -- Path to the generated thumbnail
                        view_image_path TEXT,  -- Path to the cached image for display
                        exif_data TEXT,  -- JSON string for full EXIF data
                        mtime REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                ''')
                
                # Indexes for better performance
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_file_path ON image_metadata(file_path)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_path_hash ON image_metadata(path_hash)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_content_hash ON image_metadata(content_hash)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_rating ON image_metadata(rating)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_camera_make ON image_metadata(camera_make)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_camera_model ON image_metadata(camera_model)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_date_taken ON image_metadata(date_taken)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_file_size ON image_metadata(file_size)
                ''')
                # thumbnail_path and view_image_path are never used as query predicates;
                # drop their indexes to reduce write overhead.
                cursor.execute('DROP INDEX IF EXISTS idx_thumbnail_path')
                cursor.execute('DROP INDEX IF EXISTS idx_view_image_path')

                # Migration: add sidecars column (JSON array of sidecar paths)
                try:
                    cursor.execute("ALTER TABLE image_metadata ADD COLUMN sidecars TEXT DEFAULT '[]'")
                except sqlite3.OperationalError:
                    pass  # Column already exists

                # Tag system: normalized junction table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'keyword'
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS image_tags (
                        file_path TEXT NOT NULL,
                        tag_id INTEGER NOT NULL,
                        PRIMARY KEY (file_path, tag_id),
                        FOREIGN KEY (file_path) REFERENCES image_metadata(file_path) ON DELETE CASCADE,
                        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
                    )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_image_tags_file ON image_tags(file_path)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id)')

                self.conn.commit()
                
                logging.info(f"Metadata database initialized: {self.db_path}")
                
            except sqlite3.Error as e:
                logging.error(f"Error initializing metadata database: {e}")
                raise
                
    def _get_metadata_hash(self, file_path: str, stat_result: Optional[os.stat_result] = None) -> Optional[str]:
        """Calculates a fast MD5 hash based on file path, size, and modification time."""
        try:
            stat_info = stat_result or os.stat(file_path)
            info = f"{file_path}-{stat_info.st_size}-{stat_info.st_mtime_ns}"
            return hashlib.md5(info.encode('utf-8')).hexdigest()
        except OSError as e:
            logging.warning(f"Could not stat file {file_path} to generate metadata hash: {e}")
            return None

    @staticmethod
    def _build_entry(file_path: str, sidecars_json: str = "[]") -> ImageEntry:
        """Construct an ImageEntry from DB columns."""
        try:
            sidecars = tuple(json.loads(sidecars_json)) if sidecars_json else ()
        except (json.JSONDecodeError, TypeError):
            sidecars = ()
        return ImageEntry(path=file_path, sidecars=sidecars)

    def update_sidecars(self, file_path: str, sidecars: List[str]) -> None:
        """Store sidecar paths for a file."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE image_metadata SET sidecars = ? WHERE file_path = ?",
                    (json.dumps(sidecars), file_path),
                )
                self.conn.commit()
        except sqlite3.Error as e:
            logging.debug(f"Error updating sidecars for {file_path}: {e}")

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
            with self._lock:
                cursor = self.conn.cursor()
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
            logging.debug(f"Error in get_metadata_batch: {e}")
        return results

    def get_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Gets all metadata for a file strictly from the database. This method is
        guaranteed to be fast and non-blocking. It may return stale data if the
        file has been modified since the last background scan.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
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
            logging.debug(f"Error getting metadata for {file_path}: {e}")
            return None
            
    def extract_and_store_metadata(self, file_path: str):
        """
        Extracts metadata from a file and stores it in the database.
        This method is intended to be called from a background worker.
        """
        try:
            st = os.stat(file_path)
        except OSError:
            logging.warning(f"File not found for metadata extraction: {file_path}")
            return
        metadata = self._extract_metadata_from_file(file_path, file_size=st.st_size)
        self._store_metadata(file_path, metadata, st.st_mtime, stat_result=st)
        logging.debug(f"Metadata extracted and stored for: {file_path}")

    def needs_full_metadata(self, file_path: str) -> bool:
        """Returns True if the row is missing rich EXIF fields (camera, dimensions, etc.)."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
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

    def extract_and_store_fast_metadata(self, file_path: str):
        """Plugin binary scan for orientation/rating/file_size only.
        UPDATE is scoped to fast fields; rich EXIF columns are untouched."""
        _, ext = os.path.splitext(file_path)
        plugin = plugin_registry.get_plugin_for_format(ext)
        if not plugin or not hasattr(plugin, 'extract_metadata'):
            return
        try:
            # why: plugins are user-supplied; any exception must not abort the metadata pipeline
            plugin_meta = plugin.extract_metadata(file_path)
        except Exception as e:
            logging.debug(f"Fast metadata extraction failed for {file_path}: {e}")
            return
        if plugin_meta is None:
            return

        try:
            st = os.stat(file_path)
            mtime = st.st_mtime
            file_size = st.st_size
        except OSError:
            return

        orientation = plugin_meta.get('orientation', 1)
        rating = plugin_meta.get('rating', 0)
        path_hash = self._get_metadata_hash(file_path, stat_result=st)
        current_time = time.time()

        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    'SELECT id, rating, updated_at FROM image_metadata WHERE file_path = ?',
                    (file_path,),
                )
                existing = cursor.fetchone()

                if existing:
                    if existing[2] and existing[2] > mtime:
                        rating = existing[1]
                    cursor.execute('''
                        UPDATE image_metadata SET
                            path_hash = ?, file_size = ?, orientation = ?,
                            rating = ?, mtime = ?, updated_at = ?
                        WHERE id = ?
                    ''', (path_hash, file_size, orientation, rating, mtime,
                          current_time, existing[0]))
                else:
                    cursor.execute('''
                        INSERT INTO image_metadata
                        (file_path, path_hash, file_size, orientation, rating,
                         mtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, file_size, orientation, rating,
                          mtime, current_time, current_time))
                self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            logging.error(f"Error storing fast metadata for {file_path}: {e}")

    def extract_and_store_full_metadata(self, file_path: str):
        """Runs the exiftool path (skipping the plugin fast path) and stores all fields."""
        try:
            st = os.stat(file_path)
        except OSError:
            logging.warning(f"File not found for full metadata extraction: {file_path}")
            return
        metadata = self._extract_metadata_from_file(file_path, _use_plugin=False, file_size=st.st_size)
        self._store_metadata(file_path, metadata, st.st_mtime, stat_result=st)
        logging.debug(f"Full metadata extracted and stored for: {file_path}")

    def _extract_metadata_from_file(self, file_path: str, _use_plugin: bool = True, file_size: Optional[int] = None) -> Dict[str, Any]:
        """
        Extracts all metadata from a file using exiftool.
        It uses a plugin-specific override if available for performance.
        """
        # --- Plugin Override ---
        _, ext = os.path.splitext(file_path)
        plugin = plugin_registry.get_plugin_for_format(ext)
        if _use_plugin and plugin and hasattr(plugin, 'extract_metadata'):
            try:
                plugin_meta = plugin.extract_metadata(file_path)
                if plugin_meta is not None:
                    logging.debug(f"Using fast metadata extractor from plugin '{plugin.__class__.__name__}' for {os.path.basename(file_path)}")
                    # Initialize with defaults, then update with plugin data.
                    metadata = {
                        'rating': 0, 'file_size': 0, 'width': 0, 'height': 0,
                        'camera_make': None, 'camera_model': None, 'lens_model': None,
                        'focal_length': None, 'aperture': None, 'shutter_speed': None,
                        'iso': None, 'date_taken': None, 'orientation': 1,
                        'color_space': None, 'thumbnail_path': None, 'view_image_path': None,
                        'exif_data': {}
                    }
                    metadata.update(plugin_meta)
                    if file_size is not None:
                        metadata['file_size'] = file_size
                    else:
                        try:
                            metadata['file_size'] = os.path.getsize(file_path)
                        except OSError:
                            pass
                    return metadata
            except Exception as e:  # why: plugins are user-supplied; any exception must not abort the metadata pipeline
                logging.error(f"Plugin extractor '{plugin.__class__.__name__}' failed for {file_path}: {e}. Falling back to default.")

        # --- Default Exiftool Fallback ---
        metadata = {
            'rating': 0,
            'file_size': 0,
            'width': 0,
            'height': 0,
            'camera_make': None,
            'camera_model': None,
            'lens_model': None,
            'focal_length': None,
            'aperture': None,
            'shutter_speed': None,
            'iso': None,
            'date_taken': None,
            'orientation': 1,
            'color_space': None,
            'thumbnail_path': None,  # Path to the generated thumbnail
            'view_image_path': None,  # Path to the cached image for display
            'exif_data': {}
        }
        
        try:
            metadata['file_size'] = file_size if file_size is not None else os.path.getsize(file_path)

            # Extract EXIF data via the persistent exiftool process (avoids per-file startup cost).
            raw = _get_fallback_exiftool().execute(['-json', '-all', '-XMP:Rating', file_path])
            exif_data = json.loads(raw)
            if exif_data and len(exif_data) > 0:
                data = exif_data[0]

                # Extract rating (prefer XMP:Rating, otherwise Rating)
                if 'XMP:Rating' in data:
                    try:
                        metadata['rating'] = int(float(data['XMP:Rating']))
                    except (ValueError, TypeError):
                        pass
                elif 'Rating' in data: # Fallback for older or other tags
                    try:
                        metadata['rating'] = int(float(data['Rating']))
                    except (ValueError, TypeError):
                        pass

                # Image dimensions
                if 'ImageWidth' in data:
                    try:
                        metadata['width'] = int(data['ImageWidth'])
                    except (ValueError, TypeError):
                        pass
                if 'ImageHeight' in data:
                    try:
                        metadata['height'] = int(data['ImageHeight'])
                    except (ValueError, TypeError):
                        pass

                # Camera information
                metadata['camera_make'] = data.get('Make')
                metadata['camera_model'] = data.get('Model')
                metadata['lens_model'] = data.get('LensModel')

                # Capture parameters
                if 'FocalLength' in data:
                    try:
                        focal_str = str(data['FocalLength']).replace('mm', '').strip()
                        metadata['focal_length'] = float(focal_str)
                    except (ValueError, TypeError):
                        pass

                if 'FNumber' in data:
                    try:
                        metadata['aperture'] = float(data['FNumber'])
                    except (ValueError, TypeError):
                        pass

                metadata['shutter_speed'] = data.get('ShutterSpeed')

                if 'ISO' in data:
                    try:
                        metadata['iso'] = int(data['ISO'])
                    except (ValueError, TypeError):
                        pass

                # Date taken
                for date_field in ['DateTimeOriginal', 'CreateDate', 'DateTime']:
                    if date_field in data:
                        metadata['date_taken'] = data[date_field]
                        break

                # Orientation
                if 'Orientation' in data:
                    try:
                        metadata['orientation'] = int(data['Orientation'])
                    except (ValueError, TypeError):
                        pass

                # Color space
                metadata['color_space'] = data.get('ColorSpace')

                # Extract keywords/tags from XMP:Subject and IPTC:Keywords
                keywords = set()
                for field in ('Subject', 'Keywords'):
                    val = data.get(field)
                    if isinstance(val, list):
                        keywords.update(str(v) for v in val if v)
                    elif isinstance(val, str) and val:
                        keywords.add(val)
                if keywords:
                    metadata['_keywords'] = list(keywords)

                # Store full EXIF data
                metadata['exif_data'] = data

        except (TimeoutError, RuntimeError, json.JSONDecodeError, ValueError, FileNotFoundError) as e:
            logging.debug(f"Error extracting metadata from {file_path}: {e}")
        
        return metadata
    
    def set_thumbnail_paths(self, file_path: str, thumbnail_path: Optional[str] = None, view_image_path: Optional[str] = None) -> bool:
        """
        Sets the thumbnail and view image paths for a file.
        """
        try:
            current_time = time.time()
            # Stat outside the lock to avoid blocking other DB operations on NAS.
            try:
                st = os.stat(file_path)
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
                         mtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, st.st_size, thumbnail_path, view_image_path,
                          st.st_mtime, current_time, current_time))

                self.conn.commit()
                logging.debug(f"Committed thumbnail paths for {file_path}. Rows affected: {cursor.rowcount}")
                return True

        except sqlite3.Error as e:
            logging.error(f"Error setting thumbnail paths for {file_path}: {e}", exc_info=True)
            return False
    
    def get_thumbnail_paths(self, file_path: str) -> Dict[str, str]:
        """
        Gets the thumbnail and view image paths for a file.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                
                cursor.execute('''
                    SELECT thumbnail_path, view_image_path FROM image_metadata 
                    WHERE file_path = ?
                ''', (file_path,))
                
                result = cursor.fetchone()
                
                if result:
                    return {
                        'thumbnail_path': result[0],
                        'view_image_path': result[1]
                    }
                    
        except sqlite3.Error as e:
            logging.error(f"Error getting thumbnail paths for {file_path}: {e}")
            
        return {'thumbnail_path': None, 'view_image_path': None}

    def batch_get_thumbnail_validity(self, file_paths: List[str]) -> Dict[str, Dict]:
        """
        Batch-checks thumbnail validity for multiple files in a single query.
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
                stat_cache[p] = os.stat(p)
            except OSError:
                pass  # file gone — will be treated as invalid

        try:
            placeholders = ",".join("?" for _ in file_paths)
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(f'''
                    SELECT file_path, thumbnail_path, view_image_path, mtime, file_size
                    FROM image_metadata
                    WHERE file_path IN ({placeholders})
                ''', file_paths)
                for row in cursor.fetchall():
                    fp, thumb, view, stored_mtime, stored_size = row
                    stat = stat_cache.get(fp)
                    valid = (
                        stat is not None
                        and stored_mtime is not None
                        and stored_mtime >= stat.st_mtime
                        and stored_size == stat.st_size
                        and thumb
                        and os.path.exists(thumb)
                    )
                    results[fp] = {
                        'thumbnail_path': thumb,
                        'view_image_path': view,
                        'valid': bool(valid),
                    }
        except sqlite3.Error as e:
            logging.error(f"Error in batch_get_thumbnail_validity: {e}")

        return results

    def get_cached_thumbnail_paths(self, file_path: str) -> Optional[Dict[str, str]]:
        """Returns cached thumbnail/view paths without stat-ing the source file.

        Trusts the DB record — only verifies the local thumbnail file exists.
        Returns None if no cached thumbnail is available.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT thumbnail_path, view_image_path FROM image_metadata
                    WHERE file_path = ?
                ''', (file_path,))
                result = cursor.fetchone()

            if result and result[0] and os.path.exists(result[0]):
                return {
                    'thumbnail_path': result[0],
                    'view_image_path': result[1],
                }
        except sqlite3.Error as e:
            logging.error(f"Error in get_cached_thumbnail_paths for {file_path}: {e}")
        return None

    def batch_get_cached_thumbnail_validity(self, file_paths: List[str]) -> Dict[str, Dict]:
        """Batch trust-cache check — no os.stat() on source files.

        Like batch_get_thumbnail_validity but skips mtime/size validation
        against the source file.  Only checks that the DB has a record and
        the local thumbnail file exists.
        """
        if not file_paths:
            return {}

        results: Dict[str, Dict] = {}
        try:
            placeholders = ",".join("?" for _ in file_paths)
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute(f'''
                    SELECT file_path, thumbnail_path, view_image_path
                    FROM image_metadata
                    WHERE file_path IN ({placeholders})
                ''', file_paths)
                for row in cursor.fetchall():
                    fp, thumb, view = row
                    valid = bool(thumb and os.path.exists(thumb))
                    results[fp] = {
                        'thumbnail_path': thumb,
                        'view_image_path': view,
                        'valid': valid,
                    }
        except sqlite3.Error as e:
            logging.error(f"Error in batch_get_cached_thumbnail_validity: {e}")

        return results

    def is_thumbnail_valid(self, file_path: str) -> bool:
        """
        Checks if a valid thumbnail exists for the file. This is optimized to reduce syscalls.
        """
        try:
            # Combine file existence check, mtime, and size into a single os.stat call for efficiency.
            stat_info = os.stat(file_path)
            mtime = stat_info.st_mtime
            file_size = stat_info.st_size

            with self._lock:
                cursor = self.conn.cursor()

                # Select only the columns needed for validation to reduce data transfer.
                cursor.execute('''
                    SELECT thumbnail_path, mtime, file_size FROM image_metadata
                    WHERE file_path = ?
                ''', (file_path,))

                result = cursor.fetchone()

                if result:
                    thumbnail_path, stored_mtime, stored_file_size = result

                    # Check modification time, file size, and existence of the thumbnail file.
                    if (stored_mtime >= mtime and
                        stored_file_size == file_size and
                        thumbnail_path and
                        os.path.exists(thumbnail_path)):
                        return True

        except FileNotFoundError:
            # If os.stat fails, the file doesn't exist, so the thumbnail is not valid.
            return False
        except sqlite3.Error as e:
            logging.error(f"Error checking thumbnail validity for {file_path}: {e}")

        return False
        
    def _store_metadata(self, file_path: str, metadata: Dict[str, Any], mtime: float, stat_result: Optional[os.stat_result] = None):
        """Stores metadata in the database."""
        try:
            path_hash = self._get_metadata_hash(file_path, stat_result=stat_result)
            current_time = time.time()
            
            # Serialize EXIF data as JSON
            exif_json = json.dumps(metadata.get('exif_data', {}))
            
            with self._lock:
                cursor = self.conn.cursor()

                # Check for an existing entry to decide whether to INSERT or UPDATE
                cursor.execute('SELECT id, thumbnail_path, view_image_path, content_hash, rating, updated_at FROM image_metadata WHERE file_path = ?', (file_path,))
                existing_row = cursor.fetchone()

                # Preserve existing paths to avoid race conditions from other tasks
                if existing_row:
                    if not metadata.get('thumbnail_path'):
                        metadata['thumbnail_path'] = existing_row[1]
                    if not metadata.get('view_image_path'):
                        metadata['view_image_path'] = existing_row[2]
                    if not metadata.get('content_hash'):
                        metadata['content_hash'] = existing_row[3]
                    # Preserve user-set rating: if the DB row was updated after
                    # the file was last modified, the rating was set explicitly
                    # (e.g. via set_rating) and the EXIF write-back may not have
                    # completed yet.  Don't overwrite it with stale EXIF data.
                    # why: mtime has 1s granularity on HFS+/APFS, so a rating
                    # set within the same second as a file write could be missed.
                    # In practice this is rare: set_rating is user-initiated and
                    # file writes are background tasks that don't coincide.
                    existing_updated_at = existing_row[5]
                    if existing_updated_at and existing_updated_at > mtime:
                        metadata['rating'] = existing_row[4]

                if existing_row:
                    # UPDATE the existing row
                    cursor.execute('''
                        UPDATE image_metadata SET
                            path_hash = ?, content_hash = ?, file_size = ?, width = ?, height = ?,
                            rating = ?,
                            camera_make = ?, camera_model = ?, lens_model = ?, focal_length = ?, aperture = ?,
                            shutter_speed = ?, iso = ?, date_taken = ?, orientation = ?, color_space = ?,
                            thumbnail_path = ?, view_image_path = ?, exif_data = ?, mtime = ?, updated_at = ?
                        WHERE id = ?
                    ''', (
                        path_hash, metadata.get('content_hash'), metadata.get('file_size', 0), metadata.get('width', 0), metadata.get('height', 0),
                        metadata.get('rating', 0),
                        metadata.get('camera_make'), metadata.get('camera_model'),
                        metadata.get('lens_model'), metadata.get('focal_length'), metadata.get('aperture'),
                        metadata.get('shutter_speed'), metadata.get('iso'), metadata.get('date_taken'),
                        metadata.get('orientation', 1), metadata.get('color_space'), metadata.get('thumbnail_path'),
                        metadata.get('view_image_path'), exif_json, mtime, current_time, existing_row[0]
                    ))
                else:
                    # INSERT a new row
                    cursor.execute('''
                        INSERT INTO image_metadata 
                        (file_path, path_hash, content_hash, file_size, width, height, rating, camera_make, camera_model, lens_model, focal_length, aperture, shutter_speed, iso, date_taken, orientation, color_space, thumbnail_path, view_image_path, exif_data, mtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        file_path, path_hash, metadata.get('content_hash'), metadata.get('file_size', 0), metadata.get('width', 0), metadata.get('height', 0),
                        metadata.get('rating', 0), metadata.get('camera_make'), metadata.get('camera_model'), metadata.get('lens_model'),
                        metadata.get('focal_length'), metadata.get('aperture'), metadata.get('shutter_speed'), metadata.get('iso'),
                        metadata.get('date_taken'), metadata.get('orientation', 1), metadata.get('color_space'),
                        metadata.get('thumbnail_path'), metadata.get('view_image_path'), exif_json, mtime, current_time, current_time
                    ))
                
                self.conn.commit()
                logging.debug(f"Committed full metadata for {file_path}. Rows affected: {cursor.rowcount}")

        except sqlite3.Error as e:
            self.conn.rollback()
            logging.error(f"Error storing metadata for {file_path}: {e}", exc_info=True)

        # Populate tag junction table from EXIF keywords (outside the main
        # transaction so a tag-write failure doesn't roll back metadata).
        exif_keywords = metadata.get('_keywords')
        if exif_keywords:
            self.add_image_tags(file_path, exif_keywords)

    def set_rating(self, file_path: str, rating: int) -> bool:
        """
        Sets a rating for a file *only* in the database.
        This method is fast and does not block the UI.
        """
        try:
            current_time = time.time()

            with self._lock:
                cursor = self.conn.cursor()

                cursor.execute('SELECT id FROM image_metadata WHERE file_path = ?', (file_path,))
                
                if cursor.fetchone():
                    # Update existing entry
                    logging.debug(f"Updating rating for {os.path.basename(file_path)} to {rating} in DB.")
                    cursor.execute('''
                        UPDATE image_metadata 
                        SET rating = ?, updated_at = ?
                        WHERE file_path = ?
                    ''', (rating, current_time, file_path))
                else:
                    # Create new entry with minimal metadata
                    logging.debug(f"Inserting new DB entry for {os.path.basename(file_path)} with rating {rating}.")
                    st = os.stat(file_path)
                    path_hash = self._get_metadata_hash(file_path, stat_result=st)
                    file_size = st.st_size
                    mtime = st.st_mtime
                    
                    cursor.execute('''
                        INSERT INTO image_metadata 
                        (file_path, path_hash, file_size, rating, mtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (file_path, path_hash, file_size, rating, mtime, current_time, current_time))
                
                self.conn.commit()
                rowcount = cursor.rowcount
                
                if rowcount > 0:
                    logging.info(f"Successfully set rating for {os.path.basename(file_path)} to {rating}. Rows affected: {rowcount}.")
                else:
                    logging.warning(f"DB transaction for rating on {os.path.basename(file_path)} completed, but no rows were affected.")
                return True
                
        except sqlite3.Error as e:
            logging.error(f"Error setting rating for {file_path} in database: {e}", exc_info=True)
            return False

    def batch_set_ratings(self, file_paths: List[str], rating: int) -> tuple:
        """
        Sets a rating for a batch of files in a single transaction.
        Returns (success: bool, count: int) — count is the number of files actually written.
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
                                stat = os.stat(path)
                                info = f"{path}-{stat.st_size}-{stat.st_mtime_ns}"
                                path_hash = hashlib.md5(info.encode('utf-8')).hexdigest()
                                insert_data.append((
                                    path, path_hash, stat.st_size, rating, stat.st_mtime,
                                    current_time, current_time
                                ))
                            except OSError as e:
                                logging.warning(f"Could not stat file for batch insert: {path}, {e}")
                                skipped += 1

                        if insert_data:
                            cursor.executemany('''
                                INSERT INTO image_metadata
                                (file_path, path_hash, file_size, rating, mtime, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            ''', insert_data)

            written = len(paths_to_process) - skipped
            logging.info(f"Successfully batch-set rating for {written}/{len(file_paths)} files to {rating}.")
            return (skipped == 0, written)
        except sqlite3.Error as e:
            logging.error(f"Error in batch_set_ratings for {len(file_paths)} files: {e}", exc_info=True)
            return (False, 0)
            
    def get_files_by_rating(self, rating: int) -> List[str]:
        """
        Gets all files with a specific rating.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT file_path FROM image_metadata
                    WHERE rating = ?
                    ORDER BY updated_at DESC
                ''', (rating,))
                results = cursor.fetchall()

            return [row[0] for row in results]

        except sqlite3.Error as e:
            logging.error(f"Error getting files by rating {rating}: {e}")
            return []

    def search_by_camera(self, make: Optional[str] = None, model: Optional[str] = None) -> List[str]:
        """
        Searches for images by camera make and/or model.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
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
                results = cursor.fetchall()

            return [row[0] for row in results]
                
        except sqlite3.Error as e:
            logging.error(f"Error searching by camera: {e}")
            return []
            
    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool],
                               tag_names: Optional[List[str]] = None) -> List[str]:
        """
        Gets file paths that match the text, star, and tag filters
        by performing the filtering directly within the database.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()

                query = "SELECT file_path FROM image_metadata WHERE 1=1"
                params: list = []

                # Add text filter
                if text_filter:
                    query += " AND file_path LIKE ?"
                    params.append(f"%{text_filter}%")

                # Add star filter
                enabled_ratings = [i for i, state in enumerate(star_states) if state]
                if len(enabled_ratings) < len(star_states) and enabled_ratings:
                    placeholders = ", ".join("?" for _ in enabled_ratings)
                    query += f" AND rating IN ({placeholders})"
                    params.extend(enabled_ratings)
                elif not enabled_ratings:
                    # If no ratings are selected, match no files
                    query += " AND 1=0"

                # Add tag filter
                if tag_names:
                    tag_placeholders = ", ".join("?" for _ in tag_names)
                    query += f""" AND file_path IN (
                        SELECT it.file_path FROM image_tags it
                        JOIN tags t ON t.id = it.tag_id
                        WHERE t.name IN ({tag_placeholders})
                    )"""
                    params.extend(tag_names)

                cursor.execute(query, params)
                results = cursor.fetchall()

                return [row[0] for row in results]

        except sqlite3.Error as e:
            logging.error(f"Error getting filtered files: {e}", exc_info=True)
            return []

    def get_all_file_paths(self) -> List[str]:
        """Gets a list of all file_path entries from the database."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('SELECT file_path FROM image_metadata')
                # fetchall returns a list of tuples, so we unpack them
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error(f"Error getting all file paths from database: {e}")
            return []

    def get_directory_files(self, directory_path: str, recursive: bool = False) -> List[str]:
        """Gets file paths from the DB for a directory.

        When *recursive* is True, returns all files under *directory_path*
        (including subdirectories).  When False, returns only direct children.
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                search_path = os.path.join(directory_path, '')
                if recursive:
                    cursor.execute(
                        "SELECT file_path FROM image_metadata WHERE file_path LIKE ?",
                        (search_path + '%',),
                    )
                else:
                    cursor.execute("""
                        SELECT file_path FROM image_metadata
                        WHERE file_path LIKE ? AND SUBSTR(file_path, LENGTH(?) + 1) NOT LIKE '%/%'
                    """, (search_path + '%', search_path))
                files = [row[0] for row in cursor.fetchall()]
                return files
        except sqlite3.Error as e:
            logging.error(f"Failed to get directory files for {directory_path} from DB: {e}")
            return []

    def batch_ensure_records_exist(self, file_paths: List[str]):
        """
        Efficiently creates minimal DB records for a list of files if they don't already exist.
        """
        if not file_paths:
            return

        current_time = time.time()
        records_to_insert = []

        with self._lock:
            with self.conn:  # Transaction
                cursor = self.conn.cursor()

                placeholders = ','.join('?' * len(file_paths))
                cursor.execute(f'SELECT file_path FROM image_metadata WHERE file_path IN ({placeholders})', file_paths)
                existing_paths = {row[0] for row in cursor.fetchall()}
                new_paths = [p for p in file_paths if p not in existing_paths]

        if not new_paths:
            return

        # Stat outside the lock to avoid blocking DB on NAS round-trips
        logging.info(f"Batch inserting {len(new_paths)} new minimal records into database.")
        for path in new_paths:
            try:
                st = os.stat(path)
                path_hash = self._get_metadata_hash(path, stat_result=st)
                records_to_insert.append((
                    path, path_hash, st.st_size, st.st_mtime,
                    current_time, current_time
                ))
            except OSError:
                continue

        if records_to_insert:
            with self._lock:
                with self.conn:
                    cursor = self.conn.cursor()
                    cursor.executemany("""
                        INSERT OR IGNORE INTO image_metadata (file_path, path_hash, file_size, mtime, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, records_to_insert)

    def remove_records(self, file_paths: List[str]) -> bool:
        """
        Removes image records from the database and their associated cache files.
        """
        if not file_paths:
            return True

        try:
            with self._lock:
                with self.conn:  # Automatic transaction
                    cursor = self.conn.cursor()

                    # 1. Get cache paths before deleting records
                    placeholders = ','.join('?' for _ in file_paths)
                    cursor.execute(f'''
                        SELECT thumbnail_path, view_image_path FROM image_metadata
                        WHERE file_path IN ({placeholders})
                    ''', file_paths)
                    cache_paths_to_delete = cursor.fetchall()

                    # 2. Delete records from the database
                    cursor.execute(f'''
                        DELETE FROM image_metadata WHERE file_path IN ({placeholders})
                    ''', file_paths)
                    rows_affected = cursor.rowcount
                    logging.info(f"Deleted {rows_affected} records from database for {len(file_paths)} files.")

            # 3. Delete associated cache files outside the DB lock
            for thumb_path, view_path in cache_paths_to_delete:
                for path in (thumb_path, view_path):
                    if path:
                        try:
                            os.remove(path)
                            logging.debug(f"Removed cache file: {path}")
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            logging.warning(f"Error removing cache file {path}: {e}")
            
            return True

        except sqlite3.Error as e:
            logging.error(f"Error removing records for {len(file_paths)} files: {e}", exc_info=True)
            return False

    def cleanup_missing_files(self):
        """
        Removes entries for files that no longer exist.
        This operation can be time-consuming and should not be called when quitting the app quickly.
        """
        try:
            # Fetch all paths while holding the lock, then release it before doing filesystem I/O.
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('SELECT file_path FROM image_metadata')
                all_paths = [row[0] for row in cursor.fetchall()]

            # Filesystem existence checks happen outside the lock to avoid blocking DB operations.
            missing_paths = [p for p in all_paths if not os.path.exists(p)]

            if missing_paths:
                with self._lock:
                    cursor = self.conn.cursor()
                    cursor.executemany(
                        'DELETE FROM image_metadata WHERE file_path = ?',
                        [(path,) for path in missing_paths]
                    )
                    self.conn.commit()
                logging.info(f"Cleaned up {len(missing_paths)} missing files from metadata database")

        except sqlite3.Error as e:
            logging.error(f"Error cleaning up metadata database: {e}")


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

                self.conn.commit()
                if cursor.rowcount > 0:
                    logging.debug(f"Set content_hash for {os.path.basename(file_path)}")
                else:
                    logging.warning(f"Could not set content_hash for {os.path.basename(file_path)}, file path not found in DB.")
                return True
        except sqlite3.Error as e:
            logging.error(f"Error setting content hash for {file_path}: {e}")
            return False

    def move_records(self, moves: List[Dict[str, str]]) -> int:
        """
        Atomically renames file_path entries for moved files.

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
            logging.info(f"move_records: updated {updated}/{len(moves)} rows.")
        except sqlite3.Error as e:
            logging.error(f"Error in move_records: {e}", exc_info=True)
        return updated

    # ──────────────────────────────────────────────────────────────────────
    #  Tag CRUD
    # ──────────────────────────────────────────────────────────────────────

    def get_or_create_tag(self, name: str, kind: str = 'keyword') -> int:
        """Returns the tag id, creating the row if needed. Caller must hold _lock."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM tags WHERE name = ?', (name,))
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute('INSERT INTO tags (name, kind) VALUES (?, ?)', (name, kind))
        return cursor.lastrowid

    def add_image_tags(self, file_path: str, tag_names: List[str]) -> None:
        """Adds tags to an image without removing existing ones."""
        if not tag_names:
            return
        try:
            with self._lock:
                with self.conn:
                    for name in tag_names:
                        tag_id = self.get_or_create_tag(name)
                        self.conn.execute(
                            'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                            (file_path, tag_id),
                        )
        except sqlite3.Error as e:
            logging.error(f"Error adding tags for {file_path}: {e}")

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
            logging.error(f"Error removing tags for {file_path}: {e}")

    def set_image_tags(self, file_path: str, tag_names: List[str]) -> None:
        """Replaces all tags for an image with the given list."""
        try:
            with self._lock:
                with self.conn:
                    self.conn.execute('DELETE FROM image_tags WHERE file_path = ?', (file_path,))
                    for name in tag_names:
                        tag_id = self.get_or_create_tag(name)
                        self.conn.execute(
                            'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                            (file_path, tag_id),
                        )
        except sqlite3.Error as e:
            logging.error(f"Error setting tags for {file_path}: {e}")

    def batch_set_tags(self, file_paths: List[str], tag_names: List[str]) -> bool:
        """Adds tags to multiple images in a single transaction."""
        if not file_paths or not tag_names:
            return False
        try:
            with self._lock:
                with self.conn:
                    tag_ids = [self.get_or_create_tag(name) for name in tag_names]
                    for fp in file_paths:
                        for tid in tag_ids:
                            self.conn.execute(
                                'INSERT OR IGNORE INTO image_tags (file_path, tag_id) VALUES (?, ?)',
                                (fp, tid),
                            )
            return True
        except sqlite3.Error as e:
            logging.error(f"Error in batch_set_tags: {e}")
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
            logging.error(f"Error in batch_remove_tags: {e}")
            return False

    def get_image_tags(self, file_path: str) -> List[str]:
        """Returns tag names for a single image."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT t.name FROM tags t
                    JOIN image_tags it ON it.tag_id = t.id
                    WHERE it.file_path = ?
                    ORDER BY t.name
                ''', (file_path,))
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error(f"Error getting tags for {file_path}: {e}")
            return []

    def get_all_tags(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all tags as [{id, name, kind}], optionally filtered by kind."""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                if kind:
                    cursor.execute('SELECT id, name, kind FROM tags WHERE kind = ? ORDER BY name', (kind,))
                else:
                    cursor.execute('SELECT id, name, kind FROM tags ORDER BY name')
                return [{'id': r[0], 'name': r[1], 'kind': r[2]} for r in cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error(f"Error getting all tags: {e}")
            return []

    def get_directory_tags(self, directory_path: str) -> List[Dict[str, Any]]:
        """Returns tags used by images under a directory (for autocomplete prioritization)."""
        try:
            search_path = os.path.join(directory_path, '')
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT DISTINCT t.id, t.name, t.kind FROM tags t
                    JOIN image_tags it ON it.tag_id = t.id
                    WHERE it.file_path LIKE ?
                    ORDER BY t.name
                ''', (search_path + '%',))
                return [{'id': r[0], 'name': r[1], 'kind': r[2]} for r in cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error(f"Error getting directory tags for {directory_path}: {e}")
            return []

    def close(self):
        """Closes the database connection."""
        with self._lock:
            if self.conn:
                self.conn.close()
                logging.info(f"Metadata database connection closed: {self.db_path}")

# Global database instance
_metadata_database: Optional[MetadataDatabase] = None
_metadata_database_lock = Lock()

def get_metadata_database(db_path: str) -> MetadataDatabase:
    """Gets (or lazily creates) the global metadata database instance."""
    global _metadata_database
    if _metadata_database is None:
        with _metadata_database_lock:
            if _metadata_database is None:
                _metadata_database = MetadataDatabase(db_path)
    return _metadata_database
