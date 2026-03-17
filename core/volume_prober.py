import hashlib
import logging
import os
import pathlib
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class VolumeProber:
    """NAS/network volume accessibility probing and file header reading.

    Extracted from ThumbnailManager to separate infrastructure concerns
    (volume reachability, file I/O) from image-processing domain logic.
    """

    def __init__(self, config_manager):
        self._remote_paths: List[str] = config_manager.get("remote_paths", [])
        self._volume_cache: Dict[str, Tuple[bool, float]] = {}
        self._volume_cache_lock = threading.Lock()

    def get_mount_point(self, path: str) -> Optional[str]:
        """Return the remote mount prefix for *path*, or None for local paths.

        Checks ``remote_paths`` config first, then falls back to the macOS
        ``/Volumes/X`` heuristic.
        """
        for prefix in self._remote_paths:
            try:
                pathlib.PurePath(path).relative_to(prefix)
                return prefix
            except ValueError:
                continue
        # why: macOS mounts network shares and external volumes under /Volumes/<name>
        parts = pathlib.PurePath(path).parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return str(pathlib.Path(parts[0]) / parts[1] / parts[2])
        return None

    def is_accessible(self, path: str, timeout: float = 2.0) -> bool:
        """Returns False if the volume containing *path* does not respond
        within *timeout* seconds. Results are cached per mount point for 60 s.
        Local paths always return True without probing.
        Callers that return early on False do not requeue the skipped task;
        the file will be processed again only on the next scan or watchdog event.
        """
        mount_point = self.get_mount_point(path)
        if mount_point is None:
            return True

        now = time.time()
        with self._volume_cache_lock:
            cached = self._volume_cache.get(mount_point)
            if cached is not None and now < cached[1]:
                return cached[0]

        responded = threading.Event()
        def _probe():
            try:
                os.stat(mount_point)  # disk-io: volume accessibility probe
                responded.set()
            except OSError:
                pass   # event stays unset; timeout path handles it

        threading.Thread(target=_probe, daemon=True).start()
        accessible = responded.wait(timeout)

        with self._volume_cache_lock:
            self._volume_cache[mount_point] = (accessible, now + 60.0)

        if not accessible:
            logger.warning("Volume inaccessible (timeout %.1fs): %s — skipping task.", timeout, mount_point)
        return accessible

    def hash_file(self, file_path: str) -> Optional[str]:
        """Generate MD5 hash of the first 256KB of the file for performance.
        Reads only 256KB — callers that also need the prefetch buffer should
        call read_file_header directly to avoid a second I/O round-trip.
        """
        result = self.read_file_header(file_path, prefetch_size=256 * 1024)
        return result[0] if result else None

    def read_file_header(self, file_path: str, prefetch_size: int = 512 * 1024) -> Optional[Tuple[str, bytes]]:
        """Read the first *prefetch_size* bytes of *file_path* in a single syscall.

        Returns ``(md5_of_first_256KB, header_bytes)`` so callers can both
        identify the file and inspect its binary structure without a second NAS
        round-trip.  Returns ``None`` on error.
        """
        start_time = time.time()
        try:
            with open(file_path, "rb") as f:  # disk-io: prefetch header read
                header = f.read(prefetch_size)

            # Hash only the first 256 KB so the digest stays compatible with
            # thumbnails already on disk from previous runs.
            hash_chunk = header[:256 * 1024]
            md5 = hashlib.md5(hash_chunk).hexdigest()

            duration = time.time() - start_time
            logger.debug(f"read_file_header {os.path.basename(file_path)}: {len(header)} B in {duration:.4f}s")
            return md5, header
        except OSError as e:
            duration = time.time() - start_time
            logger.error(f"VolumeProber: Error reading header of {file_path} after {duration:.4f}s: {e}")
            return None
