import logging
import subprocess
import sys
import threading
import time

from core.event_system import event_system, EventType, EventData
from gui.modal_menu import MenuNode

logger = logging.getLogger(__name__)


def _thumbnail_only(ctx):
    return ctx.view == "thumbnail"


def _publish(event_type):
    def _fire():
        event_system.publish(EventData(
            event_type=event_type, source="menu", timestamp=time.time(),
        ))
    return _fire


# -- Bookmark menu helpers ------------------------------------------------

def _build_bookmark_children(node, operation: str, script_api_ref: list):
    from core.bookmark_manager import load_bookmarks
    bookmarks = load_bookmarks()
    node.children = [
        MenuNode(
            label=bm.name,
            key=bm.key,
            action=_bookmark_action(bm.path, operation, script_api_ref),
        )
        for bm in bookmarks
    ]


def _bookmark_action(dest_dir: str, operation: str, script_api_ref: list):
    def _do():
        # why: ScriptAPI methods use _on_main_thread() which deadlocks if
        # called from the Qt main thread; run in a background thread like scripts.
        threading.Thread(target=_run, daemon=True,
                         name=f"bookmark-{operation}").start()

    def _run():
        api = script_api_ref[0]
        if api is None:
            return
        selected = api.get_selected_images()
        if not selected:
            return
        all_paths = api.expand_group_paths(sorted(selected))
        if not all_paths:
            return
        op_name = f"bookmark_{operation}"
        api.daemon_tasks([(op_name, all_paths, {"dest_dir": dest_dir})])
        from core.event_system import StatusMessageEventData
        n = len(all_paths)
        verb = "Copying" if operation == "copy" else "Moving"
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE, source="bookmark",
            timestamp=time.time(),
            message=f"{verb} {n} file{'s' if n != 1 else ''} to {dest_dir}",
            timeout=4000,
        ))
    return _do


def _edit_bookmarks():
    from core.bookmark_manager import BOOKMARKS_PATH
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", BOOKMARKS_PATH])
        elif sys.platform == "win32":
            subprocess.Popen(["start", BOOKMARKS_PATH], shell=True)
        else:
            subprocess.Popen(["xdg-open", BOOKMARKS_PATH])
    except OSError:  # why: editor binary may not be on PATH in sandboxed/minimal environments
        logger.warning("Failed to open bookmarks file: %s", BOOKMARKS_PATH)


def build_menus(script_api_ref: list = None) -> dict:
    """Build all modal menu trees.

    *script_api_ref* is a single-element list holding the live ScriptAPI
    (set after ScriptManager init).  Bookmark actions read ``[0]`` at
    invocation time so the reference can be late-bound.
    """
    if script_api_ref is None:
        script_api_ref = [None]

    sort_menu = MenuNode("Sort", children=[
        MenuNode("Date", key="d", script="sort_by_date", visible=_thumbnail_only),
        MenuNode("Name", key="n", script="sort_by_name", visible=_thumbnail_only),
        MenuNode("Rating", key="r", script="sort_by_rating", visible=_thumbnail_only),
        MenuNode("Size", key="s", script="sort_by_size", visible=_thumbnail_only),
        MenuNode("Type", key="t", script="sort_by_type", visible=_thumbnail_only),
    ])
    tag_menu = MenuNode("Tags", children=[
        MenuNode("Add / Edit", key="a", action=_publish(EventType.OPEN_TAG_EDITOR)),
        MenuNode("Filter", key="f", action=_publish(EventType.OPEN_TAG_FILTER)),
    ])
    export_menu = MenuNode("Export", children=[
        MenuNode("JPG from RAW", key="j", script="extract_jpg"),
    ])
    rotate_menu = MenuNode("Rotate", children=[
        MenuNode("90° CW", key="3", script="rotate_90"),
        MenuNode("180°", key="1", script="rotate_180"),
        MenuNode("270° CW", key="2", script="rotate_270"),
        MenuNode("Auto-rotate (AI)", key="a", script="auto_rotate"),
    ])
    open_with_menu = MenuNode("Open with", children=[
        MenuNode("vkdt", key="v", script="open_in_vkdt"),
    ])
    compare_menu = MenuNode("Compare", children=[
        MenuNode("Grid", key="g", action=_publish(EventType.OPEN_COMPARE_GRID)),
        MenuNode("Split line", key="s", action=_publish(EventType.OPEN_COMPARE_SPLIT)),
    ])

    # Bookmark menu — children are rebuilt from YAML each time it opens.
    copy_submenu = MenuNode(
        "Copy to", key="c",
        refresh=lambda node: _build_bookmark_children(node, "copy", script_api_ref),
    )
    move_submenu = MenuNode(
        "Move to", key="m",
        refresh=lambda node: _build_bookmark_children(node, "move", script_api_ref),
    )
    bookmark_menu = MenuNode("Bookmark", children=[
        copy_submenu,
        move_submenu,
        MenuNode("Edit bookmarks", key="e", action=_edit_bookmarks),
    ])

    return {
        "sort": sort_menu, "tags": tag_menu, "export": export_menu,
        "rotate": rotate_menu, "open_with": open_with_menu,
        "compare": compare_menu, "bookmark": bookmark_menu,
    }
