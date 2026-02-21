from scripts.script_api import ScriptAPI
from typing import List
import logging

def run_script(api: ScriptAPI, selected_images: list[str] = None):
    """
    Sets the rating of the selected images to 4 stars.

    Args:
        api: The ScriptAPI instance providing access to viewer functions.
        selected_images: An optional list of image paths. If not provided,
                         the currently selected images in the viewer will be used.
    """
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logging.info("No images selected to set rating to 4 stars.")
        return

    logging.info(f"Setting rating to 4 stars for {len(selected_images)} images.")
    api.set_rating_for_images(selected_images, 4)
    logging.info("Rating set to 4 stars.")
