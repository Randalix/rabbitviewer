import logging
import os
import threading
import uuid
from collections import defaultdict
from typing import List, Generator

from core.priority import NATIVELY_VIEWABLE, Priority, RenderTask, SourceJob

logger = logging.getLogger(__name__)

# Concurrency control for AI tasks. This represents the maximum number of
# concurrent ONNX inference tasks of a given type. This prevents CPU saturation
# while keeping the worker pool free.
_clip_concurrency_sem = threading.Semaphore(2)
_face_concurrency_sem = threading.Semaphore(2)


class AITaskCoordinator:
    """Coordinates CLIP, auto-orientation, and face detection SourceJobs."""

    def __init__(self, config_manager, metadata_db, render_manager):
        self.config_manager = config_manager
        self.metadata_db = metadata_db
        self.render_manager = render_manager

    def _warm_all_models(self):
        """Warm all AI models sequentially in a background thread (daemon use).

        Runs at background OS priority (QOS_CLASS_BACKGROUND on macOS) so CoreML
        avoids GPU/ANE code paths that dispatch_sync to the main thread, keeping
        the Qt event loop responsive during the ~60s load window.
        """
        from core.model_manager import get_model_path
        from core import onnx_runtime

        onnx_runtime._set_background_priority()

        model_keys = [
            "clip-vit-b-32-visual", "clip-vit-b-32-textual",
            "face-detection-buffalo_l", "face-recognition-buffalo_l",
            "orientation-efficientnet",
        ]
        for key in model_keys:
            path = get_model_path(key, self.config_manager)
            if path:
                try:
                    onnx_runtime.get_session(path)
                except Exception:
                    logger.debug("Prewarm failed for %s", path, exc_info=True)
        logger.info("AI models pre-warmed.")

    def prewarm_all_models(self) -> None:
        """Entry point for startup scheduler to warm all AI models."""
        threading.Thread(target=self._warm_all_models, daemon=True, name="ai-prewarm").start()

    def _throttled_gated_generator(self, items: List[str], semaphore: threading.Semaphore) -> Generator[List[str], None, None]:
        """Yield items one at a time, acquiring the semaphore before each.

        Throttles the *submission* of tasks to the RenderManager so at most
        ``semaphore._value`` (initial count) AI tasks run concurrently.
        """
        for item in items:
            semaphore.acquire()
            yield [item]

    @staticmethod
    def _batched_generator(items, batch_size=10):
        """Standard generator for non-throttled tasks."""
        batch = []
        for item in items:
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    # ── CLIP embedding generation ───────────────────────────────

    def _generate_clip_embedding(self, file_path: str, cancel_event=None):
        """
        Runs inference in a worker thread. Releases the semaphore on completion
        to allow the throttled generator to submit the next task.
        """
        from core import clip_inference
        try:
            if cancel_event and cancel_event.is_set():
                return

            paths = self.metadata_db.images.get_thumbnail_paths(file_path)
            thumb = paths.get('thumbnail_path') if paths else None

            embedding = clip_inference.encode_image(
                file_path, thumbnail_path=thumb, config_manager=self.config_manager)
            # Always mark as attempted so unsupported files (e.g. firmware .CAP)
            # are not re-submitted on every launch.
            self.metadata_db.embeddings.mark_file_clip_scanned(file_path)
            self.metadata_db.ledgers.file_work_remove(file_path, 'clip')
            if embedding is None:
                logger.debug("CLIP embedding failed for %s", file_path)
                return

            blob = clip_inference.embedding_to_bytes(embedding)
            self.metadata_db.embeddings.upsert_embedding(file_path, blob)
            logger.debug("CLIP embedding stored for %s", file_path)
        finally:
            _clip_concurrency_sem.release()

    def _create_clip_embed_tasks(self, file_path: str, priority: Priority) -> List[RenderTask]:
        if not file_path or file_path == "/":
            _clip_concurrency_sem.release()
            return []
        return [
            RenderTask(
                task_id=f"clip_embed::{file_path}",
                priority=priority,
                func=self._generate_clip_embedding,
                args=(file_path,),
            )
        ]

    def submit_clip_indexing_job(self, directory: str, file_paths: List[str]):
        """Submits a job that uses a throttled generator for CLIP indexing."""
        from core import clip_inference
        if not clip_inference.is_available() or not self.config_manager.get("ai.enabled", True):
            return
        if not self.config_manager.get("ai.clip_search.enabled", True):
            return

        missing = self.metadata_db.embeddings.get_files_missing_embeddings(file_paths)
        if not missing:
            return

        logger.info("CLIP indexing: %d files to embed in %s", len(missing), directory)
        job = SourceJob(
            job_id=f"clip_index::{directory}",
            priority=Priority.CLIP_INDEX,
            task_priority=Priority.CLIP_INDEX,
            generator=self._throttled_gated_generator(missing, _clip_concurrency_sem),
            task_factory=self._create_clip_embed_tasks,
            create_tasks=True,
        )
        self.render_manager.submit_source_job(job)

    # ── Auto-orientation ────────────────────────────────────────

    def _auto_orient_task(self, file_path: str, cancel_event=None):
        """Runs in RenderManager worker thread."""
        from core import orientation_model
        # This task is not throttled as it's less resource-intensive
        if cancel_event and cancel_event.is_set():
            return

        current_orient = self.metadata_db.images.get_orientation(file_path)
        if current_orient != 1:
            self.metadata_db.ledgers.file_work_remove(file_path, 'auto_orient')
            return
        if self.metadata_db.ledgers.pending_write_exists(file_path, 'orientation'):
            self.metadata_db.ledgers.file_work_remove(file_path, 'auto_orient')
            return

        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        thumb = paths.get('thumbnail_path') if paths else None

        result = orientation_model.predict_orientation(
            file_path, thumbnail_path=thumb, config_manager=self.config_manager)
        if result is None:
            return

        orientation, confidence = result
        threshold = self.config_manager.get("ai.auto_orient.confidence_threshold", 0.9)
        if confidence < threshold or orientation == 1:
            return

        self.metadata_db.images.set_orientation(file_path, orientation)
        self.metadata_db.ledgers.file_work_remove(file_path, 'auto_orient')
        logger.info("Auto-orient: set orientation=%d (conf=%.2f) for %s",
                     orientation, confidence, file_path)

    def _create_auto_orient_tasks(self, file_path: str, priority: Priority) -> List[RenderTask]:
        if not file_path:
            return []
        return [
            RenderTask(
                task_id=f"auto_orient::{file_path}",
                priority=priority,
                func=self._auto_orient_task,
                args=(file_path,),
            )
        ]

    def submit_auto_orient_job(self, directory: str, file_paths: List[str]):
        """Submits a job that waits for the orientation model to be ready."""
        if not self.config_manager.get("ai.enabled", True):
            return
        if not self.config_manager.get("ai.auto_orient.enabled", False):
            return

        orientations = self.metadata_db.images.batch_get_orientations(file_paths)
        candidates = [fp for fp in file_paths if orientations.get(fp, 1) == 1]
        if not candidates:
            return

        logger.info("Auto-orient: %d candidates in %s", len(candidates), directory)
        job = SourceJob(
            job_id=f"auto_orient::{directory}",
            priority=Priority.CLIP_INDEX,
            task_priority=Priority.CLIP_INDEX,
            generator=self._batched_generator(candidates, batch_size=10),
            task_factory=self._create_auto_orient_tasks,
            create_tasks=True,
        )
        self.render_manager.submit_source_job(job)
        
    # ── Face detection + recognition ──────────────────────────────

    def _face_detect_task(self, file_path: str, sem, cancel_event=None):
        """Runs inference in a worker thread. Releases sem (may be None for on-demand)."""
        from core import face_inference, face_clustering
        try:
            if cancel_event and cancel_event.is_set():
                return

            paths = self.metadata_db.images.get_thumbnail_paths(file_path)
            view_img = paths.get('view_image_path') if paths else None
            ext = os.path.splitext(file_path)[1].lower()
            if not view_img and ext not in NATIVELY_VIEWABLE:
                self.metadata_db.faces.mark_file_face_scanned(file_path)
                self.metadata_db.ledgers.file_work_remove(file_path, 'face_detect')
                return

            confidence = self.config_manager.get("ai.face_recognition.detection_confidence", 0.5)
            faces = face_inference.detect_and_embed(
                file_path, view_image_path=view_img,
                config_manager=self.config_manager,
                confidence_threshold=confidence)
            # Always mark scanned so files with no faces aren't re-submitted on next launch.
            self.metadata_db.faces.mark_file_face_scanned(file_path)
            self.metadata_db.ledgers.file_work_remove(file_path, 'face_detect')
            if not faces:
                return

            threshold = self.config_manager.get("ai.face_recognition.recognition_threshold", 0.6)
            all_face_rows = self.metadata_db.faces.get_all_person_embeddings()
            by_person = defaultdict(list)
            for row in all_face_rows:
                emb = face_inference.bytes_to_embedding(row['embedding'])
                if emb is not None:
                    by_person[row['person_id']].append(emb)
            person_means = []
            for person_id, embeddings in by_person.items():
                mean = face_clustering.compute_person_mean(embeddings)
                if mean is not None:
                    person_means.append((person_id, mean))

            new_face_data = []
            for face in faces:
                face_id = str(uuid.uuid4())
                emb_bytes = face_inference.embedding_to_bytes(face['embedding'])
                self.metadata_db.faces.insert_face_detection(
                    face_id, file_path, emb_bytes, face['bbox'],
                    face['confidence'], 'buffalo_l')
                new_face_data.append((face_id, face['embedding']))

            assignments = face_clustering.assign_faces(new_face_data, person_means, threshold)
            for face_id, person_id in assignments:
                if person_id:
                    self.metadata_db.faces.assign_face_to_person(face_id, person_id)
                else:
                    new_person_id = str(uuid.uuid4())
                    self.metadata_db.faces.create_person(new_person_id, feature_face_id=face_id)
                    self.metadata_db.faces.assign_face_to_person(face_id, new_person_id)

            logger.debug("Face detection: %d faces in %s", len(faces), file_path)
        finally:
            if sem is not None:
                sem.release()

    def _create_face_detect_tasks(self, file_path: str, priority: Priority,
                                   sem: threading.Semaphore | None = _face_concurrency_sem) -> List[RenderTask]:
        """Task factory for face detection.  sem=None for on-demand (no throttle)."""
        if not file_path or file_path == "/":
            if sem is not None:
                sem.release()
            return []
        return [
            RenderTask(
                task_id=f"face_detect::{file_path}",
                priority=priority,
                func=self._face_detect_task,
                args=(file_path, sem),
            )
        ]

    def submit_face_detection_job(self, directory: str, file_paths: List[str], *,
                                   on_demand: bool = False):
        """Submit face detection tasks.

        on_demand=True: high priority, no concurrency throttle — called when the
        user explicitly requests face search with unindexed images.
        on_demand=False: background priority, throttled to 2 concurrent tasks.
        """
        from core import face_inference
        if not face_inference.is_available() or not self.config_manager.get("ai.enabled", True):
            return
        if not self.config_manager.get("ai.face_recognition.enabled", True):
            return
        if not on_demand and not self.config_manager.get("ai.face_recognition.auto_index", True):
            return

        missing = self.metadata_db.faces.get_files_missing_faces(file_paths)
        if not missing:
            return

        logger.info("Face detection (%s): %d files in %s",
                    "on-demand" if on_demand else "background", len(missing), directory)

        if on_demand:
            job = SourceJob(
                job_id=f"face_detect::{directory}",
                priority=Priority.GUI_REQUEST,
                task_priority=Priority.GUI_REQUEST,
                generator=self._batched_generator(missing, batch_size=10),
                task_factory=lambda fp, p: self._create_face_detect_tasks(fp, p, sem=None),
                create_tasks=True,
            )
        else:
            job = SourceJob(
                job_id=f"face_detect::{directory}",
                priority=Priority.CLIP_INDEX,
                task_priority=Priority.CLIP_INDEX,
                generator=self._throttled_gated_generator(missing, _face_concurrency_sem),
                task_factory=self._create_face_detect_tasks,
                create_tasks=True,
                suppress_progress=True,
            )
        self.render_manager.submit_source_job(job)
