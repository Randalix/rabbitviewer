import logging
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI, selected_images: list[str] = None):
    """Reset rotation to original orientation."""
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No images selected to reset rotation.")
        return

    logger.info(f"Resetting rotation for {len(selected_images)} images.")
    api.reset_rotation(selected_images)
