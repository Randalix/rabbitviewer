# gui/components/star_drag_context.py


class StarDragContext:
    """Shared mutable state for a group of StarButtons that drag together."""

    def __init__(self):
        self.is_active = False
        self.initial_state = False
        self.last_button = None
