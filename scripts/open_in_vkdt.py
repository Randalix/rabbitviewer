import logging
import subprocess
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)


def run_script(api: ScriptAPI, selected_images: list[str] | None = None):
    """Open the current image in vkdt."""
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No image to open in vkdt.")
        return

    path = selected_images[0]
    logger.info(f"Opening in vkdt: {path}")
    subprocess.Popen(["vkdt", path])
