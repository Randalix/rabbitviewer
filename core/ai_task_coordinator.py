import logging
import os
import uuid
from collections import defaultdict
from typing import List

from core.priority import NATIVELY_VIEWABLE, Priority, RenderTask, SourceJob

logger = logging.getLogger(__name__)


class AITaskCoordinator:
    """Coordinates CLIP, auto-orientation, and face detection SourceJobs.

    Extracted from ThumbnailManager to isolate AI-specific task scaffolding
    from image generation logic. Each AI feature follows the same pattern:
    task function, task factory, and job submission with batched generator.
    """

    def __init__(self, config_manager, metadata_db, render_manager):
        self.config_manager = config_manager
        self.metadata_db = metadata_db
        self.render_manager = render_manager

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

    # ── CLIP embedding generation ───────────────────────────────

    def _generate_clip_embedding(self, file_path: str, cancel_event=None):
        """Runs in RenderManager worker thread."""
        from core import clip_inference

        if cancel_event and cancel_event.is_set():
            return

        # Use cached thumbnail to avoid NAS read
        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        thumb = paths.get('thumbnail_path') if paths else None

        embedding = clip_inference.encode_image(
            file_path, thumbnail_path=thumb, config_manager=self.config_manager)
        if embedding is None:
            logger.debug("CLIP embedding failed for %s", file_path)
            return

        blob = clip_inference.embedding_to_bytes(embedding)
        self.metadata_db.embeddings.upsert_embedding(file_path, blob)
        self.metadata_db.ledgers.file_work_remove(file_path, 'clip')
        logger.debug("CLIP embedding stored for %s", file_path)

    def _create_clip_embed_tasks(self, file_paths, priority: Priority) -> List[RenderTask]:
        if isinstance(file_paths, str):
            file_paths = [file_paths]
        return [
            RenderTask(
                task_id=f"clip_embed::{fp}",
                priority=priority,
                func=self._generate_clip_embedding,
                args=(fp,),
            )
            for fp in file_paths
        ]

    def submit_clip_indexing_job(self, directory: str, file_paths: List[str]):
        """No-op if numpy missing or AI disabled in config."""
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
            generator=self._batched_generator(missing),
            task_factory=self._create_clip_embed_tasks,
            create_tasks=True,
        )
        self.render_manager.submit_source_job(job)

    # ── Auto-orientation ────────────────────────────────────────

    def _auto_orient_task(self, file_path: str, cancel_event=None):
        """Runs in RenderManager worker thread. Skips if orientation set or pending write."""
        from core import orientation_model

        if cancel_event and cancel_event.is_set():
            return

        # Guard: skip if orientation already set
        current_orient = self.metadata_db.images.get_orientation(file_path)
        if current_orient != 1:
            self.metadata_db.ledgers.file_work_remove(file_path, 'auto_orient')
            return

        # Guard: skip if there's a pending orientation write
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
        if confidence < threshold:
            logger.debug("Auto-orient: low confidence %.2f for %s", confidence, file_path)
            return
        if orientation == 1:
            return  # already correct

        self.metadata_db.images.set_orientation(file_path, orientation)
        self.metadata_db.ledgers.file_work_remove(file_path, 'auto_orient')
        logger.info("Auto-orient: set orientation=%d (conf=%.2f) for %s",
                     orientation, confidence, file_path)

    def _create_auto_orient_tasks(self, file_paths, priority: Priority) -> List[RenderTask]:
        if isinstance(file_paths, str):
            file_paths = [file_paths]
        return [
            RenderTask(
                task_id=f"auto_orient::{fp}",
                priority=priority,
                func=self._auto_orient_task,
                args=(fp,),
            )
            for fp in file_paths
        ]

    def submit_auto_orient_job(self, directory: str, file_paths: List[str]):
        """No-op if AI or auto_orient disabled in config."""
        if not self.config_manager.get("ai.enabled", True):
            return
        if not self.config_manager.get("ai.auto_orient.enabled", False):
            return

        # Only process files with default orientation (1 = unset)
        orientations = self.metadata_db.images.batch_get_orientations(file_paths)
        candidates = [fp for fp in file_paths if orientations.get(fp, 1) == 1]
        if not candidates:
            return

        logger.info("Auto-orient: %d candidates in %s", len(candidates), directory)

        job = SourceJob(
            job_id=f"auto_orient::{directory}",
            priority=Priority.CLIP_INDEX,
            task_priority=Priority.CLIP_INDEX,
            generator=self._batched_generator(candidates),
            task_factory=self._create_auto_orient_tasks,
            create_tasks=True,
        )
        self.render_manager.submit_source_job(job)

    # ── Face detection + recognition ──────────────────────────────

    def _face_detect_task(self, file_path: str, cancel_event=None):
        from core import face_inference, face_clustering

        if cancel_event and cancel_event.is_set():
            return

        # Use view image (full-res cache) for face detection — thumbnails are
        # too small (128px) for SCRFD's 640×640 input and ArcFace alignment.
        paths = self.metadata_db.images.get_thumbnail_paths(file_path)
        view_img = paths.get('view_image_path') if paths else None

        # Skip non-PIL-readable files (RAW formats) when no view image exists yet.
        # They'll be retried on the next scan once view images are generated.
        ext = os.path.splitext(file_path)[1].lower()
        if not view_img and ext not in NATIVELY_VIEWABLE:
            logger.debug("Face detection: skipping %s (no view image for RAW file)", file_path)
            return

        confidence = self.config_manager.get("ai.face_recognition.detection_confidence", 0.5)
        faces = face_inference.detect_and_embed(
            file_path, view_image_path=view_img,
            config_manager=self.config_manager,
            confidence_threshold=confidence)
        if not faces:
            return

        threshold = self.config_manager.get("ai.face_recognition.recognition_threshold", 0.6)

        # Single query for all person embeddings instead of N queries
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

        self.metadata_db.ledgers.file_work_remove(file_path, 'face_detect')
        logger.debug("Face detection: %d faces in %s", len(faces), file_path)

    def _create_face_detect_tasks(self, file_paths, priority: Priority) -> List[RenderTask]:
        if isinstance(file_paths, str):
            file_paths = [file_paths]
        return [
            RenderTask(
                task_id=f"face_detect::{fp}",
                priority=priority,
                func=self._face_detect_task,
                args=(fp,),
            )
            for fp in file_paths
        ]

    def submit_face_detection_job(self, directory: str, file_paths: List[str]):
        """No-op if AI or face recognition disabled in config."""
        from core import face_inference
        if not face_inference.is_available() or not self.config_manager.get("ai.enabled", True):
            return
        if not self.config_manager.get("ai.face_recognition.enabled", True):
            return
        if not self.config_manager.get("ai.face_recognition.auto_index", True):
            return

        missing = self.metadata_db.faces.get_files_missing_faces(file_paths)
        if not missing:
            return

        logger.info("Face detection: %d files to process in %s", len(missing), directory)

        job = SourceJob(
            job_id=f"face_detect::{directory}",
            priority=Priority.CLIP_INDEX,
            task_priority=Priority.CLIP_INDEX,
            generator=self._batched_generator(missing),
            task_factory=self._create_face_detect_tasks,
            create_tasks=True,
        )
        self.render_manager.submit_source_job(job)
