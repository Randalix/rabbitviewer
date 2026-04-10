import logging
import os

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu

from gui.modal_menu import MenuContext, MenuNode

logger = logging.getLogger(__name__)

# Actions that only apply in picture/video view — skip in thumbnail context menu.
_SKIP_IN_THUMBNAIL = frozenset({
    "escape_picture_view",
    "next_image",
    "previous_image",
})


def build_thumbnail_context_menu(hotkey_manager, menus, script_manager,
                                  selected_paths, hovered_path=None) -> QMenu:
    """Build a QMenu from hotkey definitions for the thumbnail right-click menu.

    Chord menus (menu:* actions) become submenus; direct actions become QActions.
    """
    file_types = set()
    paths_for_types = selected_paths or ({hovered_path} if hovered_path else set())
    for p in paths_for_types:
        _, ext = os.path.splitext(p)
        if ext:
            file_types.add(ext.lower())

    ctx = MenuContext(
        view="thumbnail",
        has_selection=bool(selected_paths),
        selection_count=len(selected_paths),
        file_types=file_types,
    )

    menu = QMenu()
    definitions = hotkey_manager.definitions
    actions = hotkey_manager.actions

    # First pass: chord menus as submenus (top of menu).
    chord_added = False
    for action_name, defn in definitions.items():
        if not action_name.startswith("menu:"):
            continue
        menu_id = action_name[5:]
        node = menus.get(menu_id)
        if not node:
            continue
        submenu = _build_node_submenu(node, ctx, script_manager, menu)
        if submenu.isEmpty():
            continue
        act = menu.addMenu(submenu)
        seq = defn.sequences[0] if defn.sequences else ""
        if seq:
            act.setShortcut(QKeySequence(seq))
            act.setShortcutVisibleInContextMenu(True)
        chord_added = True

    if chord_added:
        menu.addSeparator()

    # Second pass: direct (non-chord) actions in config order.
    for action_name, defn in definitions.items():
        if action_name in _SKIP_IN_THUMBNAIL or action_name.startswith("menu:"):
            continue

        label = defn.description or action_name
        seq = defn.sequences[0] if defn.sequences else ""

        act = QAction(label, menu)
        if seq:
            # Display-only — the real QShortcut is managed by HotkeyManager.
            act.setShortcut(QKeySequence(seq))
            act.setShortcutVisibleInContextMenu(True)
        handler = actions.get(action_name)
        if handler:
            act.triggered.connect(lambda checked=False, h=handler: h())
        menu.addAction(act)

    return menu


def _build_node_submenu(node: MenuNode, ctx: MenuContext,
                        script_manager, parent) -> QMenu:
    submenu = QMenu(node.label, parent)

    if node.refresh:
        try:
            node.refresh(node)
        except Exception:
            logger.exception("Context menu: refresh failed for '%s'", node.label)

    for child in node.children:
        if child.visible is not None and not child.visible(ctx):
            continue

        if child.children or child.refresh:
            child_sub = _build_node_submenu(child, ctx, script_manager, submenu)
            if not child_sub.isEmpty():
                submenu.addMenu(child_sub)
        elif child.action:
            act = QAction(child.label, submenu)
            if child.key:
                act.setShortcut(QKeySequence(child.key))
                act.setShortcutVisibleInContextMenu(True)
            a = child.action
            act.triggered.connect(lambda checked=False, _a=a: _a())
            submenu.addAction(act)
        elif child.script:
            act = QAction(child.label, submenu)
            if child.key:
                act.setShortcut(QKeySequence(child.key))
                act.setShortcutVisibleInContextMenu(True)
            kw = dict(child.script_kwargs)
            act.triggered.connect(
                lambda checked=False, s=child.script, k=kw: script_manager.run_script(s, **k)
            )
            submenu.addAction(act)

    return submenu
