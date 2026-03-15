import logging
import time

from core.event_system import event_system, EventType, StatusMessageEventData
from scripts.script_api import ScriptAPI

logger = logging.getLogger(__name__)


def run_script(api: ScriptAPI, *, dest_dir: str, move: bool = False):
    """Copy or move selected files to a bookmark destination."""
    selected = api.get_selected_images()
    if not selected:
        return

    all_paths = api.expand_group_paths(sorted(selected))
    if not all_paths:
        return

    op_name = "bookmark_move" if move else "bookmark_copy"

    # Submit daemon task first — remove_images blocks on the Qt main thread
    # and the script runs in a daemon thread, so a quick GUI close could
    # kill the thread before daemon_tasks is reached.
    api.daemon_tasks([(op_name, all_paths, {"dest_dir": dest_dir})])

    if move:
        api.remove_images(all_paths)

    n = len(all_paths)
    verb = "Moving" if move else "Copying"
    event_system.publish(StatusMessageEventData(
        event_type=EventType.STATUS_MESSAGE, source="bookmark",
        timestamp=time.time(),
        message=f"{verb} {n} file{'s' if n != 1 else ''} to {dest_dir}",
        timeout=4000,
    ))
