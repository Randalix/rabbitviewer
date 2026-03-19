"""Background perceptual-hash computation and near-duplicate detection."""
import logging
import os
from typing import List, Optional, Set, Tuple

from core.priority import Priority, RenderTask, SourceJob

logger = logging.getLogger(__name__)


def _dct_phash(thumbnail_path: str, hash_size: int = 8) -> Optional[int]:
    """DCT-based perceptual hash using PIL + numpy. Returns a 64-bit integer.

    Algorithm: resize to 32×32 grayscale → 2D DCT (separable FFT) →
    top-left 8×8 block → threshold against block mean → pack 64 bits.
    """
    try:
        import numpy as np
        from PIL import Image

        highfreq = hash_size * 4  # 32×32 input to DCT
        img = Image.open(thumbnail_path).convert('L').resize(  # disk-io: read local thumbnail cache
            (highfreq, highfreq), Image.LANCZOS
        )
        pixels = np.array(img, dtype=float)

        # 2D DCT-II via two vectorised 1D FFT passes (rows then columns)
        def _dct1d_rows(m):
            n = m.shape[1]
            v = np.concatenate([m, m[:, ::-1]], axis=1)
            V = np.fft.rfft(v, axis=1)[:, :n]
            k = np.arange(n)
            return np.real(np.exp(-1j * np.pi * k / (2 * n)) * V)

        dct2d = _dct1d_rows(_dct1d_rows(pixels).T).T

        # Keep only the low-frequency top-left block
        block = dct2d[:hash_size, :hash_size].flatten()
        med = block[1:].mean()  # exclude DC component
        bits = block > med

        # Store as signed int64 (SQLite INTEGER range)
        raw = int(np.packbits(bits).tobytes().hex(), 16)
        return raw - (1 << 64) if raw >= (1 << 63) else raw
    except Exception as e:
        logger.debug("pHash computation error for %s: %s", os.path.basename(thumbnail_path), e)
        return None

_THRESHOLD = 10  # max Hamming distance to consider two images near-duplicates


class PHashCoordinator:
    """Coordinates pHash SourceJobs.

    Follows the same structure as AITaskCoordinator: task function,
    task factory, batched generator, job submission.  pHash is computed
    from the locally-cached thumbnail to avoid NAS I/O.
    """

    def __init__(self, metadata_db, render_manager):
        self.metadata_db = metadata_db
        self.render_manager = render_manager

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _batched_generator(items, batch_size=10):
        batch = []
        for item in items:
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    # ── background SourceJob ──────────────────────────────────────────

    def queue_for_file(self, file_path: str) -> None:
        """Queue a single pHash task at BACKGROUND_SCAN priority.

        Called after thumbnail generation so pHash runs in a separate worker
        rather than blocking the thumbnail task.
        """
        self.render_manager.submit_task(
            f"phash::{file_path}",
            Priority.BACKGROUND_SCAN,
            self._compute_phash_task,
            file_path,
        )

    def _compute_phash_task(self, file_path: str, cancel_event=None):
        """Worker task: compute pHash from cached thumbnail and persist."""
        from core.event_system import event_system, EventType, PHashProgressEventData
        import time

        if cancel_event and cancel_event.is_set():
            return

        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        thumb = paths.get('thumbnail_path') if paths else None
        if not thumb or not os.path.exists(thumb):  # disk-io: existence check on local thumbnail
            return

        h = _dct_phash(thumb)
        if h is None:
            return
        self.metadata_db.images.set_phash(file_path, h)
        logger.debug("pHash stored for %s", os.path.basename(file_path))

        event_system.publish(PHashProgressEventData(
            event_type=EventType.PHASH_PROGRESS,
            source="phash_coordinator",
            timestamp=time.time(),
            file_path=file_path,
        ))

    def _create_phash_tasks(self, file_paths, priority: Priority) -> List[RenderTask]:
        if isinstance(file_paths, str):
            file_paths = [file_paths]
        return [
            RenderTask(
                task_id=f"phash::{fp}",
                priority=priority,
                func=self._compute_phash_task,
                args=(fp,),
            )
            for fp in file_paths
        ]

    def submit_phash_job(self, directory: str, file_paths: List[str],
                         priority: Priority = Priority.BACKGROUND_SCAN) -> None:
        """Submit a background pHash job for files that are missing a phash."""
        missing = self.metadata_db.images.get_files_missing_phash(file_paths)
        if not missing:
            return

        # Use a distinct job_id per priority tier so a GUI_REQUEST job can
        # run alongside an active BACKGROUND_SCAN job without being dropped.
        tier = "urgent" if priority >= Priority.GUI_REQUEST else "bg"
        job_id = f"phash_scan_{tier}::{directory}"

        logger.info("pHash: %d files to process in %s (priority=%s)", len(missing), directory, priority.name)

        job = SourceJob(
            job_id=job_id,
            priority=priority,
            task_priority=priority,
            generator=self._batched_generator(missing),
            task_factory=self._create_phash_tasks,
            create_tasks=True,
            suppress_progress=True,
        )
        self.render_manager.submit_source_job(job)

    # ── near-duplicate detection ──────────────────────────────────────

    @staticmethod
    def find_near_duplicates(pairs: List[Tuple[str, int]]) -> Set[str]:
        """Return the set of file paths that have at least one near-duplicate.

        Uses numpy vectorised popcount on 64-bit hashes processed in row chunks
        to keep peak memory under ~25 MB for n ≤ 6 000.
        """
        if len(pairs) < 2:
            return set()

        try:
            import numpy as np
        except ImportError:
            return _find_near_duplicates_python(pairs)

        paths = [p for p, _ in pairs]
        # Cast to uint64 so XOR/popcount work correctly on all bit patterns
        hashes = np.array([h for _, h in pairs], dtype=np.int64).view(np.uint64)
        n = len(hashes)
        is_dup = np.zeros(n, dtype=bool)

        chunk_size = 512
        for start in range(0, n, chunk_size):
            end = min(start + chunk_size, n)
            hi = hashes[start:end]                          # (chunk,)
            xor = hi[:, None] ^ hashes[None, :]             # (chunk, n) uint64
            # Reinterpret each uint64 as 8 uint8 bytes, then unpack to bits
            xor_bytes = xor.view(np.uint8).reshape(end - start, n, 8)
            counts = np.unpackbits(xor_bytes, axis=2).sum(axis=2)  # (chunk, n)
            # Mask self-comparisons
            for i in range(end - start):
                counts[i, start + i] = 255
            close = counts <= _THRESHOLD
            is_dup[start:end] |= close.any(axis=1)
            is_dup |= close.any(axis=0)

        return {paths[i] for i in range(n) if is_dup[i]}


def _find_near_duplicates_python(pairs: List[Tuple[str, int]]) -> Set[str]:
    """Pure-Python fallback (no numpy)."""
    dup_set: Set[str] = set()
    for i, (pi, hi) in enumerate(pairs):
        for pj, hj in pairs[i + 1:]:
            if bin(hi ^ hj).count('1') <= _THRESHOLD:
                dup_set.add(pi)
                dup_set.add(pj)
    return dup_set
