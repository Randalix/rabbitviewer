"""MetadataDatabase facade — composes per-domain table classes.

Callers access sub-tables directly: db.images.*, db.faces.*, db.tags.*,
db.embeddings.*, db.ledgers.*, db.cache.*

Cross-domain operations (remove_records, extract_and_store_*) live here.
"""

import logging
import os
from typing import List, Optional
from threading import Lock
from core.db.connection import create_connection
from core.db.schema import init_schema
from core.metadata_extraction import extract_metadata_from_file, extract_fast_metadata_fields
from core.db.image_table import ImageTable
from core.db.tag_table import TagTable
from core.db.face_table import FaceTable
from core.db.embedding_table import EmbeddingTable
from core.db.ledger_table import LedgerTable
from core.db.cache_table import CacheTable

logger = logging.getLogger(__name__)


class MetadataDatabase:
    """Facade composing per-domain table classes with independent connections."""

    def __init__(self, db_path: str):
        logger.info(f"Initializing MetadataDatabase with path: {db_path}")
        self.db_path = db_path
        self._lock = Lock()

        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        # Schema setup connection (also used by cross-domain queries)
        self.conn = create_connection(db_path)
        init_schema(self.conn)

        # Per-domain table classes — each with its own connection for WAL concurrency
        self.images = ImageTable(db_path)
        self.tags = TagTable(db_path)
        self.faces = FaceTable(db_path)
        self.embeddings = EmbeddingTable(db_path)
        self.ledgers = LedgerTable(db_path)
        self.cache = CacheTable(db_path)

    @property
    def embedding_generation(self) -> int:
        return self.embeddings.generation

    @property
    def face_generation(self) -> int:
        return self.faces.generation

    # ==================================================================
    #  Cross-domain facade methods
    # ==================================================================

    def extract_and_store_metadata(self, file_path: str):
        """Extract metadata from file and store in DB + tags."""
        try:
            st = os.stat(file_path)  # disk-io: metadata extraction
        except OSError:
            logger.warning(f"File not found for metadata extraction: {file_path}")
            return
        metadata = extract_metadata_from_file(file_path, file_size=st.st_size)
        keywords = self.images.store_metadata(
            file_path, metadata, st.st_mtime, stat_result=st,
            birthtime=getattr(st, 'st_birthtime', None))
        if keywords:
            self.tags.add_image_tags(file_path, keywords)
        logger.debug(f"Metadata extracted and stored for: {file_path}")

    def extract_and_store_fast_metadata(self, file_path: str):
        """Plugin binary scan for orientation/rating/file_size only."""
        fields = extract_fast_metadata_fields(file_path)
        if fields is None:
            return
        self.images.store_fast_metadata(file_path, fields)

    def extract_and_store_full_metadata(self, file_path: str):
        """Exiftool path (skipping plugin fast path), stores all fields."""
        try:
            st = os.stat(file_path)  # disk-io: full metadata
        except OSError:
            logger.warning(f"File not found for full metadata extraction: {file_path}")
            return
        metadata = extract_metadata_from_file(file_path, use_plugin=False, file_size=st.st_size)
        keywords = self.images.store_metadata(
            file_path, metadata, st.st_mtime, stat_result=st,
            birthtime=getattr(st, 'st_birthtime', None))
        if keywords:
            self.tags.add_image_tags(file_path, keywords)
        logger.debug(f"Full metadata extracted and stored for: {file_path}")

    def remove_records(self, file_paths: List[str]) -> bool:
        """Remove image records, auxiliary table rows, and cache files."""
        if not file_paths:
            return True
        try:
            rows_affected, cache_paths = self.images.delete(file_paths)
            logger.info(f"Deleted {rows_affected} records for {len(file_paths)} files.")

            self.ledgers.delete_for_files(file_paths)
            self.embeddings.delete_for_files(file_paths)
            self.faces.delete_for_files(file_paths)

            for thumb_path, view_path in cache_paths:
                for path in (thumb_path, view_path):
                    if path:
                        try:
                            os.remove(path)
                            logger.debug(f"Removed cache file: {path}")
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            logger.warning(f"Error removing cache file {path}: {e}")
            return True
        except Exception as e:
            logger.error(f"Error removing records for {len(file_paths)} files: {e}", exc_info=True)
            return False

    def get_total_cache_size(self) -> int:
        """Query cache paths and stat files to get total size."""
        rows = self.cache.get_cache_paths()
        total = 0
        for thumb, view in rows:
            for path in (thumb, view):
                if path:
                    try:
                        total += os.path.getsize(path)  # disk-io: cache size accounting
                    except OSError:
                        pass
        return total

    def evict_lru_cache(self, target_bytes: int) -> int:
        """Evict LRU cache entries to bring total under target_bytes."""
        rows = self.cache.get_eviction_candidates()

        record_sizes: list[tuple[str, int]] = []
        current_total = 0
        for file_path, thumb, view in rows:
            size = 0
            for path in (thumb, view):
                if path:
                    try:
                        size += os.path.getsize(path)  # disk-io: cache size accounting
                    except OSError:
                        pass
            record_sizes.append((file_path, size))
            current_total += size

        if current_total <= target_bytes:
            return 0

        excess = current_total - target_bytes
        bytes_to_free = 0
        paths_to_remove: list[str] = []
        for file_path, size in record_sizes:
            if bytes_to_free >= excess:
                break
            paths_to_remove.append(file_path)
            bytes_to_free += size

        if paths_to_remove:
            logger.info(
                f"Cache eviction: removing {len(paths_to_remove)} LRU records "
                f"to free ~{bytes_to_free / (1024*1024):.1f} MB"
            )
            self.remove_records(paths_to_remove)

        return bytes_to_free

    def close(self):
        """Close all database connections."""
        self.images.close()
        self.tags.close()
        self.faces.close()
        self.embeddings.close()
        self.ledgers.close()
        self.cache.close()
        with self._lock:
            if self.conn:
                self.conn.close()
                logger.info(f"Metadata database connection closed: {self.db_path}")


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
