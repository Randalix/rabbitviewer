from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtGui import QPixmap, QImage
from typing import Dict, List, Optional
import logging
import os
import queue
import time

# NEU: Import des zentralen ThumbnailManager
from core.thumbnail_manager import ThumbnailManager as CoreThumbnailManager


class ThumbnailCache:
    """Manages in-memory caching of thumbnails"""

    def __init__(self, max_size=5000):
        self._cache: Dict[str, QPixmap] = {}
        self._max_size = max_size
        self._access_order: List[str] = []  # LRU tracking

    def get(self, key: str) -> Optional[QPixmap]:
        if key in self._cache:
            # Update access order (move to end)
            self._access_order.remove(key)
            self._access_order.append(key)
            return self._cache[key]
        return None

    def put(self, key: str, pixmap: QPixmap):
        if len(self._cache) >= self._max_size:
            # Remove least recently used item
            lru_key = self._access_order.pop(0)
            del self._cache[lru_key]

        self._cache[key] = pixmap.copy()
        self._access_order.append(key)

    def clear(self):
        self._cache.clear()
        self._access_order.clear()

    def __contains__(self, key):
        return key in self._cache


class ThumbnailManager(QObject):
    """Manages thumbnail loading, caching, and updates"""
    
    thumbnailReady = Signal(str, str, QPixmap)  # original_path, thumb_path, pixmap
    
    # NEU: Akzeptiert den zentralen ThumbnailManager anstelle des socket_client
    def __init__(self, core_thumbnail_manager: CoreThumbnailManager, cache_size=5000):
        super().__init__()
        self.core_thumbnail_manager = core_thumbnail_manager
        self.cache = ThumbnailCache(cache_size)
        self.pending_thumbnails = {}  # path -> priority
        self.loader_thread = None # Dieser LoaderThread wird vom ThumbnailViewWidget verwaltet
        
    def request_thumbnail(self, path: str, priority: int = 0):
        """Request a thumbnail for the given path"""
        if path in self.cache:
            # Emit immediately if cached
            pixmap = self.cache.get(path)
            self.thumbnailReady.emit(path, path, pixmap)
            return
            
        self.pending_thumbnails[path] = priority
        
    def request_thumbnails_batch(self, paths_with_priorities: List[tuple]):
        """Request multiple thumbnails with priorities"""
        for path, priority in paths_with_priorities:
            if path not in self.cache:
                self.pending_thumbnails[path] = priority
                
        # NEU: Anstatt an den Socket-Client zu senden, direkt den zentralen ThumbnailManager anfragen
        for path, prio in paths_with_priorities:
            # Hier wird die Priorität auf GUI_REQUEST gesetzt, da es eine explizite Anforderung der GUI ist
            self.core_thumbnail_manager.request_thumbnail(path, priority=True)
        
    def on_thumbnail_ready(self, original_path: str, thumb_path: str, pixmap: QPixmap):
        """Handle thumbnail ready from loader"""
        self.cache.put(original_path, pixmap)
        if original_path in self.pending_thumbnails:
            del self.pending_thumbnails[original_path]
        self.thumbnailReady.emit(original_path, thumb_path, pixmap)
        
    def clear_pending(self):
        """Clear all pending thumbnail requests"""
        self.pending_thumbnails.clear()
        
    def get_cached_thumbnail(self, path: str) -> Optional[QPixmap]:
        """Get thumbnail from cache if available"""
        return self.cache.get(path)
