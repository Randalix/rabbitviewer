import logging
import threading
from typing import Dict, Optional, List
from collections import OrderedDict


class MetadataCache:
    """Client-side LRU cache for image metadata fetched from the daemon.

    Populated by the existing hover-prefetch flow. InfoPanels read from
    it without daemon round-trips.
    """

    MAX_ENTRIES = 2000

    def __init__(self, socket_client):
        self._socket_client = socket_client
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: str) -> Optional[dict]:
        """Return cached metadata for path, or None if not cached."""
        with self._lock:
            if path in self._cache:
                self._cache.move_to_end(path)
                return self._cache[path]
        return None

    def put(self, path: str, metadata: dict) -> None:
        with self._lock:
            self._cache[path] = metadata
            self._cache.move_to_end(path)
            while len(self._cache) > self.MAX_ENTRIES:
                self._cache.popitem(last=False)

    def put_batch(self, metadata_map: Dict[str, dict]) -> None:
        with self._lock:
            for path, meta in metadata_map.items():
                self._cache[path] = meta
                self._cache.move_to_end(path)
            while len(self._cache) > self.MAX_ENTRIES:
                self._cache.popitem(last=False)

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)

    def fetch_and_cache(self, paths: List[str]) -> Dict[str, dict]:
        """Fetch from daemon, populate cache, return results.
        Called from background threads only.
        """
        try:
            resp = self._socket_client.get_metadata_batch(paths)
            if resp and hasattr(resp, 'metadata'):
                self.put_batch(resp.metadata)
                return resp.metadata
        except Exception as e:
            logging.debug(f"MetadataCache fetch failed: {e}")
        return {}
