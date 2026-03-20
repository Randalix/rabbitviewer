import logging
import os
import shutil
import threading
import time

from core.event_system import event_system, EventType, StatusMessageEventData, StatusSection

logger = logging.getLogger(__name__)


class CacheSizeManager:
    """When the limit is reached, background scans are paused (callers check
    ``is_cache_full()``) and old entries are evicted via the DB's LRU query.
    GUI-driven requests bypass the full check and instead call
    ``record_cache_write()`` which triggers eviction reactively.

    A filesystem-level safeguard also pauses writes when the disk holding
    *cache_dir* exceeds ``max_disk_usage_pct`` (default 90 %).
    """

    # Evict down to 90 % of max to avoid thrashing at the boundary.
    _HEADROOM_RATIO = 0.90

    # How often (seconds) to re-probe actual disk usage.
    _DISK_CHECK_INTERVAL = 30.0

    def __init__(self, metadata_db, max_cache_size_mb: int,
                 cache_dir: str = "", max_disk_usage_pct: float = 90.0):
        self._db = metadata_db
        self._max_bytes = max_cache_size_mb * 1024 * 1024 if max_cache_size_mb > 0 else 0
        self._current_bytes = 0
        self._lock = threading.Lock()
        self._evicting = False
        self._enabled = self._max_bytes > 0
        self._notified_full = False

        # Disk-space safeguard
        self._cache_dir = os.path.expanduser(cache_dir) if cache_dir else ""
        self._max_disk_usage_pct = max_disk_usage_pct
        self._disk_full = False
        self._disk_check_time = 0.0
        self._notified_disk_full = False

        if self._enabled:
            logger.info("CacheSizeManager: limit=%d MB, computing current size in background", max_cache_size_mb)
            # why: get_total_cache_size() stats every cached file (~80K syscalls);
            # running it on the main thread blocks startup by ~1.5s. Workers are
            # idle during this window and the first thumbnail writes arrive several
            # seconds later, so the brief under-count is harmless.
            threading.Thread(target=self._init_refresh, args=(max_cache_size_mb,), daemon=True).start()
        else:
            logger.info("CacheSizeManager: no cache size limit configured")

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def current_bytes(self) -> int:
        with self._lock:
            return self._current_bytes

    def is_disk_full(self) -> bool:
        """Return True when the filesystem holding the cache exceeds the
        configured usage threshold.  Result is cached for
        ``_DISK_CHECK_INTERVAL`` seconds to avoid excessive syscalls."""
        if not self._cache_dir:
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._disk_check_time < self._DISK_CHECK_INTERVAL:
                return self._disk_full
        # Check outside the lock — statvfs is fast but no reason to hold it.
        pct = 0.0
        try:
            usage = shutil.disk_usage(self._cache_dir)
            pct = (usage.used / usage.total) * 100.0 if usage.total else 0.0
            full = pct >= self._max_disk_usage_pct
        except OSError:
            full = False
        with self._lock:
            self._disk_full = full
            self._disk_check_time = now
            if not full:
                self._notified_disk_full = False
        if full:
            self._notify_disk_full(pct)
        return full

    def _notify_disk_full(self, pct: float) -> None:
        if self._notified_disk_full:
            return
        self._notified_disk_full = True
        logger.warning("Disk usage %.1f%% exceeds %.0f%% threshold — pausing cache writes",
                        pct, self._max_disk_usage_pct)
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="cache_size_manager",
            timestamp=time.time(),
            message=f"Disk {pct:.0f}% full — cache writes paused",
            section=StatusSection.PROCESS,
            timeout=10000,
        ))

    def is_cache_full(self) -> bool:
        if self.is_disk_full():
            return True
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

    def _notify_cache_full(self) -> None:
        if self._notified_full:
            return
        self._notified_full = True
        max_mb = self._max_bytes // (1024 * 1024)
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="cache_size_manager",
            timestamp=time.time(),
            message=f"⚠️ Cache full ({max_mb} MB) — evicting old entries",
            section=StatusSection.PROCESS,
            timeout=10000,
        ))

    def _evict_if_needed(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._current_bytes < self._max_bytes or self._evicting:
                return
            self._evicting = True
        self._notify_cache_full()
        try:
            target = int(self._max_bytes * self._HEADROOM_RATIO)
            freed = self._db.evict_lru_cache(target)
            if freed > 0:
                logger.info("CacheSizeManager: evicted %d MB", freed // (1024 * 1024))
            # why: resync from disk regardless of partial failure to avoid
            # _current_bytes drifting permanently above the limit
            self.refresh()
            with self._lock:
                if self._current_bytes < self._max_bytes:
                    self._notified_full = False
        finally:
            with self._lock:
                self._evicting = False

    def _init_refresh(self, max_cache_size_mb: int) -> None:
        self.refresh()
        with self._lock:
            current_mb = self._current_bytes // (1024 * 1024)
        logger.info("CacheSizeManager: limit=%d MB, current=%d MB", max_cache_size_mb, current_mb)

    def refresh(self) -> None:
        total = self._db.get_total_cache_size()
        with self._lock:
            self._current_bytes = total
            # Force a fresh disk check on next query.
            self._disk_check_time = 0.0
