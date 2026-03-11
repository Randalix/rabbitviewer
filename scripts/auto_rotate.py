import logging
import time
from scripts.script_api import ScriptAPI
from core.event_system import event_system, EventType, StatusMessageEventData

logger = logging.getLogger(__name__)


def _status(msg, timeout=5000):
    event_system.publish(StatusMessageEventData(
        event_type=EventType.STATUS_MESSAGE, source="auto_rotate",
        timestamp=time.time(), message=msg, timeout=timeout,
    ))


def run_script(api: ScriptAPI, selected_images: list[str] = None):
    from core import orientation_model, onnx_runtime

    if not onnx_runtime.is_available():
        _status("Auto-rotate requires onnxruntime (pip install onnxruntime)")
        return

    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No images selected for auto-rotate.")
        return

    config_manager = api.service.config_manager
    db = api.service.db
    threshold = config_manager.get("ai.auto_orient.confidence_threshold", 0.9)

    # Batch-fetch thumbnail paths and orientations to avoid N×lock acquisitions
    thumb_validity = db.batch_get_cached_thumbnail_validity(selected_images)
    current_orientations = db.batch_get_orientations(selected_images)

    corrected = []
    orientations = []
    for path in selected_images:
        # Use cached thumbnail to avoid NAS reads
        info = thumb_validity.get(path)
        thumb = info.get('thumbnail_path') if info and info.get('valid') else None

        result = orientation_model.predict_orientation(
            path, thumbnail_path=thumb, config_manager=config_manager)
        if result is None:
            logger.debug("Auto-rotate: prediction failed for %s", path)
            continue

        predicted_orient, confidence = result
        if confidence < threshold:
            logger.debug("Auto-rotate: low confidence %.2f for %s", confidence, path)
            continue

        if predicted_orient == current_orientations.get(path, 1):
            continue

        corrected.append(path)
        orientations.append(predicted_orient)
        logger.info("Auto-rotate: %s → orientation %d (conf=%.2f)",
                     path, predicted_orient, confidence)

    if corrected:
        api.service.set_absolute_orientation(corrected, orientations)
        api.invalidate_thumbnails(corrected)
        _status(f"Auto-rotated {len(corrected)}/{len(selected_images)} images")
    else:
        _status(f"No orientation corrections needed ({len(selected_images)} checked)")
