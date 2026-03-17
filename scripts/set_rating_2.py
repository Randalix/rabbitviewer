from scripts.script_api import ScriptAPI
import logging

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI, selected_images: list[str] = None):
    """
    Sets the rating of the selected images to 2 stars.

    Args:
        api: The ScriptAPI instance providing access to viewer functions.
        selected_images: An optional list of image paths. If not provided,
                         the currently selected images in the viewer will be used.
    """
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No images selected to set rating to 2 stars.")
        return

    logger.info(f"Setting rating to 2 stars for {len(selected_images)} images.")
    api.set_rating_for_images(selected_images, 2)
    api.show_overlay(selected_images, "stars", {"count": 2}, duration=1200, overlay_id="rating")
    logger.info("Rating set to 2 stars.")
