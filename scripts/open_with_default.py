import logging
from scripts.script_api import ScriptAPI
from core.platform_open import open_with_default_app

logger = logging.getLogger(__name__)


def run_script(api: ScriptAPI):
    """Open selected images with the OS default application."""
    paths = list(api.get_selected_images())
    if not paths:
        logger.info("open_with_default: no images selected")
        return
    for path in paths:
        open_with_default_app(path)
