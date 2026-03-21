import logging
from pathlib import Path
from scripts.script_api import ScriptAPI
from core.platform_open import open_containing_folder

logger = logging.getLogger(__name__)


def run_script(api: ScriptAPI):
    """Open the folder containing each selected image in the default file manager."""
    paths = list(api.get_selected_images())
    if not paths:
        logger.info("open_folder: no images selected")
        return
    # Open each unique parent folder once.
    seen: set[str] = set()
    for path in paths:
        folder = str(Path(path).parent)
        if folder not in seen:
            seen.add(folder)
            open_containing_folder(path)
