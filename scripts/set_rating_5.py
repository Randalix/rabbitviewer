from scripts.script_api import ScriptAPI
import logging

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI, selected_images: list[str] = None):
    """
    Sets the rating of the selected images/folders to 5 stars.

    Args:
        api: The ScriptAPI instance providing access to viewer functions.
        selected_images: An optional list of image paths. If not provided,
                         the currently selected images in the viewer will be used.
    """
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    folders = list(api.get_selected_folders())

    if not selected_images and not folders:
        logger.info("No images or folders selected to set rating to 5 stars.")
        return

    if selected_images:
        logger.info(f"Setting rating to 5 stars for {len(selected_images)} images.")
        api.set_rating_for_images(selected_images, 5)
        api.show_overlay(selected_images, "stars", {"count": 5}, duration=2000, overlay_id="rating")

    if folders:
        logger.info(f"Setting rating to 5 stars for {len(folders)} folders.")
        api.set_rating_for_folders(folders, 5)

    logger.info("Rating set to 5 stars.")
