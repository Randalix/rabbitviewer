from gui.modal_menu import MenuNode


def _thumbnail_only(ctx):
    return ctx.view == "thumbnail"


def build_menus() -> dict:
    sort_menu = MenuNode("Sort", children=[
        MenuNode("Date", key="d", script="sort_by_date", visible=_thumbnail_only),
        MenuNode("Name", key="n", script="sort_by_name", visible=_thumbnail_only),
        MenuNode("Rating", key="r", script="sort_by_rating", visible=_thumbnail_only),
        MenuNode("Size", key="s", script="sort_by_size", visible=_thumbnail_only),
        MenuNode("Type", key="t", script="sort_by_type", visible=_thumbnail_only),
    ])
    return {"sort": sort_menu}
