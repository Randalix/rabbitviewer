import logging
import subprocess
import sys
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

def _build_bookmark_children(node, operation: str):
    from core.bookmark_manager import load_bookmarks
    bookmarks = load_bookmarks()
    move = (operation == "move")
    node.children = [
        MenuNode(
            label=bm.name,
            key=bm.key,
            script="bookmark_transfer",
            script_kwargs={"dest_dir": bm.path, "move": move},
        )
        for bm in bookmarks
    ]


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


def build_menus() -> dict:
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
        refresh=lambda node: _build_bookmark_children(node, "copy"),
    )
    move_submenu = MenuNode(
        "Move to", key="m",
        refresh=lambda node: _build_bookmark_children(node, "move"),
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
