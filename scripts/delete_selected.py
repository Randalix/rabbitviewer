import logging
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)

def run_script(api: ScriptAPI):
    """Delete selected images from view, filesystem, and database."""
    selected = api.get_selected_images()
    if not selected:
        logger.info("No images selected for deletion")
        return

    selected_list = sorted(list(selected))

    # Expand to include paired RAW files when group mode is active.
    all_paths = api.expand_group_paths(selected_list)
    count = len(all_paths)

    logger.info(f"Removing {count} images:")
    for path in all_paths:
        logger.info(f"  - {path}")

    # 1. Remove from view for immediate UI feedback.
    api.remove_images(all_paths)

    # 2. Delegate filesystem + DB cleanup to the daemon (non-blocking).
    if not api.daemon_tasks([
        ("send2trash", all_paths),
        ("remove_records", all_paths),
    ]):
        logger.error("Failed to queue daemon deletion tasks")
