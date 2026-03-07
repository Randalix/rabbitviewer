"""Short-lived cache for source-file existence and stat results.

Worker tasks in ThumbnailManager each independently call ``os.path.exists``
or ``os.stat`` on the NAS source file.  A single file can generate 3-4 tasks,
so the same path gets stat'd 3-4 times within seconds.  This cache
deduplicates those checks with a configurable TTL (default 30 s).

Thread-safe: uses a simple ``threading.Lock`` around a dict.
"""
import os
import threading
import time
from typing import Optional


class SourceExistsCache:
    """Per-process cache; entries expire after *ttl* seconds."""

    def __init__(self, ttl: float = 30.0):
        self._ttl = ttl
        self._cache: dict[str, tuple[bool, float, Optional[os.stat_result]]] = {}
        self._lock = threading.Lock()

    def exists(self, path: str) -> bool:
        """Check existence via stat — populates the full stat cache on hit."""
        return self.stat(path) is not None

    def stat(self, path: str) -> Optional[os.stat_result]:
        """Return cached stat result, or call os.stat and cache on miss.

        Returns None if the file doesn't exist or can't be stat'd.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None and now < entry[1]:
                return entry[2]

        try:
            st = os.stat(path)
        except OSError:
            with self._lock:
                self._cache[path] = (False, now + self._ttl, None)
            return None

        with self._lock:
            self._cache[path] = (True, now + self._ttl, st)
        return st

    def put(self, path: str, stat_result: os.stat_result) -> None:
        """Pre-populate cache from a known stat result (e.g. from DirEntry.stat)."""
        now = time.monotonic()
        with self._lock:
            self._cache[path] = (True, now + self._ttl, stat_result)

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
