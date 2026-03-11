import logging
import os
import shutil
import subprocess
from pathlib import Path
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

    # When group mode is active the selection contains the JPG primary,
    # but vkdt needs the RAW file.  Resolve the group and prefer RAW.
    if api.is_group_mode():
        groups = api.get_selected_groups()
        if groups and groups[0].raw_files:
            path = groups[0].raw_files[0]

    vkdt_bin = shutil.which("vkdt")
    if not vkdt_bin:
        # GUI apps on macOS get a minimal PATH; check common user locations.
        for candidate in (Path.home() / ".local/bin/vkdt", Path("/opt/homebrew/bin/vkdt")):
            if candidate.is_file() and os.access(candidate, os.X_OK):  # disk-io: local binary lookup
                vkdt_bin = str(candidate)
                break
    if not vkdt_bin:
        logger.error("vkdt not found on PATH or in ~/.local/bin, /opt/homebrew/bin")
        return

    logger.info(f"Opening in vkdt: {path}")
    subprocess.Popen([vkdt_bin, path])
