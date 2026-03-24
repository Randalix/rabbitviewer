import logging
import shutil
import subprocess
from pathlib import Path
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI):
    """Open selected images with mpv."""

    paths = list(api.get_selected_images())
    if not paths:
        logger.info("open_with_default: no images selected")
        return

    # 1. Try to find 'mpv' in the system PATH
    mpv_bin = shutil.which("mpv")

    # 2. If not found, check the Homebrew fallback (and others if you like)
    if not mpv_bin:
        fallbacks = [
            "/opt/homebrew/bin/mpv",
            "/usr/local/bin/mpv",
            str(Path.home() / ".local/bin/mpv")
        ]
        for candidate in fallbacks:
            if shutil.which(candidate):
                mpv_bin = candidate
                break

    # 3. Final safety check
    if not mpv_bin:
        logger.error("mpv not found in PATH or common fallback locations.")
        return

    # 4. Execute
    for path in paths:
        logger.info(f"Opening with mpv: {path}")
        subprocess.Popen([mpv_bin, path])
