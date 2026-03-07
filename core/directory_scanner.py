import os
import stat
import logging
logger = logging.getLogger(__name__)
import time
import fnmatch
from dataclasses import dataclass, field
from typing import List, Set, Optional
from core.thumbnail_manager import ThumbnailManager
from core.source_cache import SourceExistsCache


@dataclass
class ReconcileContext:
    """Mutable context for scan_incremental_reconcile.

    *db_file_set* is mutated during iteration — files found on disk are
    discarded.  After the generator is exhausted, *ghost_files* contains
    DB entries that no longer exist on disk.  *discovered_files* accumulates
    every file found during the walk for post-scan task creation.
    """
    db_file_set: Set[str]
    ghost_files: List[str] = field(default_factory=list)
    discovered_files: List[str] = field(default_factory=list)

class DirectoryScanner:

    def __init__(self, thumbnail_manager: Optional[ThumbnailManager], config_manager=None,
                 source_cache: Optional[SourceExistsCache] = None):
        self.thumbnail_manager = thumbnail_manager
        self.config_manager = config_manager
        self.source_cache = source_cache
        self.min_file_size = config_manager.get("min_file_size", 8192) if config_manager else 8192
        self.ignore_patterns = config_manager.get("ignore_patterns", ["._*"]) if config_manager else ["._*"]

    @property
    def _supported_extensions(self) -> Set[str]:
        if self.thumbnail_manager:
            return set(self.thumbnail_manager.get_supported_formats())
        return set()

    def is_supported_file(self, file_path: str, stat_result: Optional[os.stat_result] = None) -> bool:
        """Single-stat check: regular file, not ignored, supported extension, big enough.

        If *stat_result* is provided (e.g. from ``DirEntry.stat()``), no
        additional ``os.stat`` call is made — saving one NAS round-trip.
        When the check passes, the stat result is stored in the source cache
        so downstream task factories don't re-stat.
        """
        filename = os.path.basename(file_path)
        _, ext = os.path.splitext(file_path)

        # Cheap string checks first — no I/O.
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(filename, pattern):
                return False

        if ext.lower() not in self._supported_extensions:
            return False

        if stat_result is None:
            try:
                stat_result = os.stat(file_path)
            except OSError:
                return False
        if not stat.S_ISREG(stat_result.st_mode):
            return False
        if stat_result.st_size < self.min_file_size:
            return False

        # Pre-populate the source cache so _passes_pre_checks avoids re-stat.
        if self.source_cache:
            self.source_cache.put(file_path, stat_result)

        return True

    def scan_incremental(self, directory_path: str, recursive: bool = True,
                         batch_size: int = 10, skip_dirs: Optional[Set[str]] = None):
        """Generator that incrementally yields batches of supported file paths.

        *skip_dirs* is an optional set of absolute directory paths to skip
        (including their subtrees).  Used by the daemon to avoid re-walking
        directories already recorded in the scan ledger.
        """
        logger.info(f"Performing incremental scan for: {directory_path} (Recursive: {recursive}, batch_size={batch_size}, skip_dirs={len(skip_dirs) if skip_dirs else 0})")
        current_batch = []
        total_yielded = 0
        scan_start = time.monotonic()

        if not os.path.isdir(directory_path):
            logger.warning(f"Directory to scan does not exist: {directory_path}")
            return

        try:
            walk_start = time.monotonic()
            walker = os.walk(directory_path) if recursive else [(directory_path, [], os.listdir(directory_path))]
            for root, dirs, files in walker:
                if skip_dirs:
                    # Prune os.walk descent into already-walked subtrees.
                    dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip_dirs]
                    if root in skip_dirs:
                        continue
                walk_elapsed = time.monotonic() - walk_start
                logger.info(f"[chunking] scan_incremental: entering dir '{root}' ({len(files)} entries, {walk_elapsed:.3f}s since last yield/start)")
                for filename in files:
                    try:
                        full_path = os.path.join(root, filename)
                        if self.is_supported_file(full_path):
                            current_batch.append(full_path)
                            if len(current_batch) >= batch_size:
                                total_yielded += len(current_batch)
                                elapsed = time.monotonic() - scan_start
                                logger.info(f"[chunking] scan_incremental: yielding batch of {len(current_batch)} (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
                                yield current_batch
                                current_batch = []
                                walk_start = time.monotonic()
                    except OSError as e:
                        logger.debug(f"[chunking] scan_incremental: OSError on '{filename}': {e}")
                        continue
            if current_batch:
                total_yielded += len(current_batch)
                elapsed = time.monotonic() - scan_start
                logger.info(f"[chunking] scan_incremental: yielding final batch of {len(current_batch)} (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
                yield current_batch
            elapsed = time.monotonic() - scan_start
            logger.info(f"[chunking] scan_incremental: generator exhausting for '{directory_path}' (total_yielded={total_yielded}, elapsed={elapsed:.3f}s)")
        except Exception as e:  # why: os.walk can raise PermissionError or unexpected filesystem errors; must not abort the generator and stall the SourceJob
            logger.error(f"Error during directory scan of {directory_path}: {e}", exc_info=True)

    def scan_incremental_reconcile(self, directory_path: str, recursive: bool,
                                     ctx: ReconcileContext, batch_size: int = 10,
                                     skip_dirs: Optional[Set[str]] = None):
        """Like scan_incremental but also tracks ghost files via *ctx*.

        Wraps scan_incremental: for each discovered file, discards it from
        ctx.db_file_set.  After the walk finishes, any paths remaining in
        db_file_set are ghost files (in DB but deleted on disk).
        """
        for batch in self.scan_incremental(directory_path, recursive, batch_size,
                                           skip_dirs=skip_dirs):
            for f in batch:
                ctx.db_file_set.discard(f)
            ctx.discovered_files.extend(batch)
            yield batch

        # Only mark ghosts if we had formats to scan for. With 0 supported
        # extensions the scan rejects everything — treating all DB entries as
        # ghosts would cause irreversible data loss.
        if self._supported_extensions:
            ctx.ghost_files = list(ctx.db_file_set)

