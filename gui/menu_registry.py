from core.event_system import event_system, EventType, EventData
from gui.modal_menu import MenuNode
import time


def _thumbnail_only(ctx):
    return ctx.view == "thumbnail"


def _publish(event_type):
    """Returns a callable that publishes the given event."""
    def _fire():
        event_system.publish(EventData(
            event_type=event_type, source="menu", timestamp=time.time(),
        ))
    return _fire


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
    return {"sort": sort_menu, "tags": tag_menu}
