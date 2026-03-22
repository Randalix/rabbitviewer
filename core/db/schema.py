"""Database schema creation and migrations.

All CREATE TABLE, CREATE INDEX, and ALTER TABLE statements live here.
Called once at startup via init_schema().
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, indexes, and run migrations."""
    try:
        cursor = conn.cursor()

        # ── image_metadata ────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS image_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                path_hash TEXT NOT NULL,
                content_hash TEXT,
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
                thumbnail_path TEXT,
                view_image_path TEXT,
                exif_data TEXT,
                mtime REAL NOT NULL,
                birthtime REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_path ON image_metadata(file_path)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_path_hash ON image_metadata(path_hash)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON image_metadata(content_hash)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_rating ON image_metadata(rating)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_camera_make ON image_metadata(camera_make)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_camera_model ON image_metadata(camera_model)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_date_taken ON image_metadata(date_taken)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_size ON image_metadata(file_size)')

        # thumbnail_path and view_image_path are never used as query predicates;
        # drop their indexes to reduce write overhead.
        cursor.execute('DROP INDEX IF EXISTS idx_thumbnail_path')
        cursor.execute('DROP INDEX IF EXISTS idx_view_image_path')

        # Migration: add sidecars column (JSON array of sidecar paths)
        try:
            cursor.execute("ALTER TABLE image_metadata ADD COLUMN sidecars TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add accessed_at column for LRU cache eviction
        try:
            cursor.execute("ALTER TABLE image_metadata ADD COLUMN accessed_at REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_accessed_at ON image_metadata(accessed_at)')

        # Migration: add birthtime column for file creation time sorting
        try:
            cursor.execute("ALTER TABLE image_metadata ADD COLUMN birthtime REAL")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add phash column for perceptual near-duplicate detection
        try:
            cursor.execute("ALTER TABLE image_metadata ADD COLUMN phash INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_phash ON image_metadata(phash)')

        # ── tags ──────────────────────────────────────────────────────
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

        # ── scan_ledger ───────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scan_ledger (
                file_path     TEXT PRIMARY KEY,
                scan_root     TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'discovered',
                discovered_at REAL NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ledger_root_status
            ON scan_ledger(scan_root, status)
        ''')

        # ── clip_embeddings ───────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clip_embeddings (
                file_path   TEXT PRIMARY KEY,
                embedding   BLOB NOT NULL,
                model_name  TEXT NOT NULL DEFAULT 'clip-vit-b-32',
                created_at  REAL NOT NULL
            )
        ''')

        # ── pending_writes ────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_writes (
                file_path   TEXT NOT NULL,
                write_type  TEXT NOT NULL,
                payload     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (file_path, write_type)
            )
        ''')

        # ── file_work ─────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_work (
                file_path   TEXT NOT NULL,
                work_type   TEXT NOT NULL,
                scan_root   TEXT NOT NULL,
                created_at  REAL NOT NULL,
                PRIMARY KEY (file_path, work_type, scan_root)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_file_work_root_type
            ON file_work(scan_root, work_type)
        ''')

        # ── file_transfers ────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_transfers (
                source_path TEXT NOT NULL,
                dest_dir    TEXT NOT NULL,
                operation   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  REAL NOT NULL,
                PRIMARY KEY (source_path, dest_dir, operation)
            )
        ''')

        # ── face_detections ───────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_detections (
                face_id     TEXT PRIMARY KEY,
                file_path   TEXT NOT NULL,
                person_id   TEXT,
                embedding   BLOB NOT NULL,
                bbox_x      REAL NOT NULL,
                bbox_y      REAL NOT NULL,
                bbox_w      REAL NOT NULL,
                bbox_h      REAL NOT NULL,
                confidence  REAL NOT NULL,
                model_name  TEXT NOT NULL DEFAULT 'buffalo_l',
                created_at  REAL NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_face_file ON face_detections(file_path)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_face_person ON face_detections(person_id)')

        # Migration: collapse pending_deletions into file_transfers (operation='delete', dest_dir='')
        try:
            cursor.execute(
                "INSERT OR IGNORE INTO file_transfers "
                "(source_path, dest_dir, operation, status, created_at) "
                "SELECT file_path, '', 'delete', 'pending', created_at FROM pending_deletions"
            )
            cursor.execute("DROP TABLE IF EXISTS pending_deletions")
        except sqlite3.OperationalError:
            pass  # pending_deletions never existed (fresh install)

        # ── ai_scanned ────────────────────────────────────────────────
        # Tracks files that have been processed by an AI model regardless of
        # whether a positive result was found. Prevents re-scanning files that
        # had no faces, no embedding, etc. on every launch.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_scanned (
                file_path  TEXT NOT NULL,
                model_type TEXT NOT NULL,
                scanned_at REAL NOT NULL,
                PRIMARY KEY (file_path, model_type)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_scanned_file ON ai_scanned(file_path)')

        # ── persons ───────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS persons (
                person_id       TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                face_count      INTEGER NOT NULL DEFAULT 0,
                feature_face_id TEXT,
                is_hidden       BOOLEAN NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL
            )
        ''')

        conn.commit()
        logger.info("Database schema initialized")

    except sqlite3.Error as e:
        logger.error(f"Error initializing database schema: {e}")
        raise
