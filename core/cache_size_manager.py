import logging
import threading

logger = logging.getLogger(__name__)


class CacheSizeManager:
    """When the limit is reached, background scans are paused (callers check
    ``is_cache_full()``) and old entries are evicted via the DB's LRU query.
    GUI-driven requests bypass the full check and instead call
    ``record_cache_write()`` which triggers eviction reactively.
    """

    # Evict down to 90 % of max to avoid thrashing at the boundary.
    _HEADROOM_RATIO = 0.90

    def __init__(self, metadata_db, max_cache_size_mb: int):
        self._db = metadata_db
        self._max_bytes = max_cache_size_mb * 1024 * 1024 if max_cache_size_mb > 0 else 0
        self._current_bytes = 0
        self._lock = threading.Lock()
        self._evicting = False
        self._enabled = self._max_bytes > 0

        if self._enabled:
            self.refresh()
            logger.info(
                "CacheSizeManager: limit=%d MB, current=%d MB",
                max_cache_size_mb,
                self._current_bytes // (1024 * 1024),
            )
        else:
            logger.info("CacheSizeManager: no cache size limit configured")

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def current_bytes(self) -> int:
        with self._lock:
            return self._current_bytes

    def is_cache_full(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            return self._current_bytes >= self._max_bytes

    def record_cache_write(self, bytes_added: int) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._current_bytes += bytes_added
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._current_bytes < self._max_bytes or self._evicting:
                return
            self._evicting = True
        try:
            target = int(self._max_bytes * self._HEADROOM_RATIO)
            freed = self._db.evict_lru_cache(target)
            if freed > 0:
                logger.info("CacheSizeManager: evicted %d MB", freed // (1024 * 1024))
            # why: resync from disk regardless of partial failure to avoid
            # _current_bytes drifting permanently above the limit
            self.refresh()
        finally:
            with self._lock:
                self._evicting = False

    def refresh(self) -> None:
        total = self._db.get_total_cache_size()
        with self._lock:
            self._current_bytes = total
