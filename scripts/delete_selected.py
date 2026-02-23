import logging
from scripts.script_api import ScriptAPI

def run_script(api: ScriptAPI):
    """Delete selected images from view, filesystem, and database."""
    selected = api.get_selected_images()
    if not selected:
        logging.info("No images selected for deletion")
        return

    selected_list = sorted(list(selected))
    count = len(selected_list)

    logging.info(f"Removing {count} images:")
    for path in selected_list:
        logging.info(f"  - {path}")

    # 1. Remove from view for immediate UI feedback.
    api.remove_images(selected_list)

    # 2. Delegate filesystem + DB cleanup to the daemon (non-blocking).
    if not api.daemon_tasks([
        ("send2trash", selected_list),
        ("remove_records", selected_list),
    ]):
        logging.error("Failed to queue daemon deletion tasks")
