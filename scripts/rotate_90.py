import logging
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI, selected_images: list[str] = None):
    """Rotate selected images 90° clockwise."""
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No images selected to rotate.")
        return

    logger.info(f"Rotating {len(selected_images)} images 90° CW.")
    api.rotate_images(selected_images, 90)
