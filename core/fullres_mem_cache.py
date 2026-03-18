"""LRU byte-size bounded in-memory cache for fullres view images."""

import threading
from collections import OrderedDict
from typing import Optional


class FullresMemCache:
    """Thread-safe LRU cache bounded by total byte size.

    Entries are evicted oldest-first when the total stored bytes exceeds
    *max_bytes*.
    """

    def __init__(self, max_bytes: int):
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0
        self._max_bytes = max_bytes

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            # Remove old entry if present (size accounting).
            if key in self._cache:
                self._bytes -= len(self._cache.pop(key))
            self._cache[key] = data
            self._cache.move_to_end(key)
            self._bytes += len(data)
            # Evict oldest until under budget.
            while self._bytes > self._max_bytes and self._cache:
                _, evicted = self._cache.popitem(last=False)
                self._bytes -= len(evicted)

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            data = self._cache.get(key)
            if data is not None:
                self._cache.move_to_end(key)
            return data

    def invalidate(self, key: str) -> None:
        with self._lock:
            data = self._cache.pop(key, None)
            if data is not None:
                self._bytes -= len(data)
