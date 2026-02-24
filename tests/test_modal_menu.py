# tests/test_modal_menu.py
"""Tests for the modal menu system: MenuNode, MenuContext, ModalMenu, menu_registry."""
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Extended Qt stubs for ModalMenu
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    qtcore = sys.modules["PySide6.QtCore"]

    for attr, val in [
        ("FramelessWindowHint", 0x00000800),
        ("Popup", 0x00080000),
        ("WA_TranslucentBackground", 120),
        ("StrongFocus", 0x0b),
        ("Key_Escape", 0x01000000),
        ("AlignVCenter", 0x0080),
        ("AlignCenter", 0x0084),
        ("AlignLeft", 0x0001),
        ("Antialiasing", 0),
        ("Monospace", 0),
    ]:
        if not hasattr(qtcore.Qt, attr):
            setattr(qtcore.Qt, attr, val)

    if not hasattr(qtcore, "QSize"):
        class _QSize:
            def __init__(self, w=0, h=0): self._w = w; self._h = h
            def width(self): return self._w
            def height(self): return self._h
        qtcore.QSize = _QSize

    # QtGui
    qtgui = sys.modules.get("PySide6.QtGui")
    if qtgui is None:
        qtgui = types.ModuleType("PySide6.QtGui")
        sys.modules["PySide6.QtGui"] = qtgui
        sys.modules["PySide6"].QtGui = qtgui

    for name in ("QFont", "QColor", "QPainter", "QPainterPath", "QKeyEvent"):
        if not hasattr(qtgui, name):
            setattr(qtgui, name, type(name, (), {"__init__": lambda self, *a, **kw: None}))

    # QtWidgets
    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    if qtwidgets is None:
        qtwidgets = types.ModuleType("PySide6.QtWidgets")
        sys.modules["PySide6.QtWidgets"] = qtwidgets
        sys.modules["PySide6"].QtWidgets = qtwidgets

    if not hasattr(qtwidgets, "QWidget"):
        class _QWidget:
            def __init__(self, *a, **kw): pass
            def setWindowFlags(self, f): pass
            def setAttribute(self, *a): pass
            def setFocusPolicy(self, p): pass
            def setGraphicsEffect(self, e): pass
            def setFixedSize(self, s): pass
            def show(self): pass
            def hide(self): pass
            def setFocus(self): pass
            def update(self): pass
            def move(self, *a): pass
            def width(self): return 200
            def height(self): return 300
            def rect(self): return MagicMock(toRectF=MagicMock(), topLeft=MagicMock(return_value=MagicMock(x=lambda: 0, y=lambda: 0)))
            def parentWidget(self): return None
            def mapToGlobal(self, p): return MagicMock(x=lambda: 0, y=lambda: 0)
        qtwidgets.QWidget = _QWidget

    if not hasattr(qtwidgets, "QGraphicsDropShadowEffect"):
        class _QGraphicsDropShadowEffect:
            def __init__(self, *a): pass
            def setBlurRadius(self, r): pass
            def setOffset(self, *a): pass
            def setColor(self, c): pass
        qtwidgets.QGraphicsDropShadowEffect = _QGraphicsDropShadowEffect


_ensure_qt_stubs()

from gui.modal_menu import MenuNode, MenuContext, ModalMenu
from gui.menu_registry import build_menus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parent_widget(view="thumbnail", selected_paths=None, hovered=None,
                        picture_view=None, video_view=None):
    parent = MagicMock()
    parent.width.return_value = 800
    parent.height.return_value = 600
    parent.rect.return_value = MagicMock(topLeft=MagicMock(return_value=MagicMock()))
    parent.mapToGlobal.return_value = MagicMock(x=lambda: 0, y=lambda: 0)

    parent.picture_view = picture_view
    parent.video_view = video_view

    if view == "thumbnail":
        parent.stacked_widget.currentWidget.return_value = MagicMock()
    elif view == "picture":
        parent.picture_view = parent.picture_view or MagicMock()
        parent.stacked_widget.currentWidget.return_value = parent.picture_view
    elif view == "video":
        parent.video_view = parent.video_view or MagicMock()
        parent.stacked_widget.currentWidget.return_value = parent.video_view

    parent.selection_state = MagicMock()
    parent.selection_state.selected_paths = selected_paths or set()
    parent.current_hovered_image = hovered
    return parent


def _make_key_event(key_char=None, qt_key=None):
    event = MagicMock()
    if qt_key is not None:
        event.key.return_value = qt_key
    else:
        event.key.return_value = ord(key_char.upper()) if key_char else 0
    event.text.return_value = key_char or ""
    return event


def _make_menu(parent=None, menus=None):
    parent = parent or _make_parent_widget()
    script_mgr = MagicMock()
    hotkey_mgr = MagicMock()
    menus = menus or {
        "test": MenuNode("Test Menu", children=[
            MenuNode("Alpha", key="a", script="do_alpha"),
            MenuNode("Beta", key="b", script="do_beta"),
        ])
    }
    menu = object.__new__(ModalMenu)
    menu._menus = menus
    menu._script_manager = script_mgr
    menu._hotkey_manager = hotkey_mgr
    menu._breadcrumb = []
    menu._current_node = None
    menu._visible_items = []
    menu._key_map = {}
    menu._context = None

    # why: stub out QWidget methods that __init__ would call via super()
    menu.setWindowFlags = MagicMock()
    menu.setAttribute = MagicMock()
    menu.setFocusPolicy = MagicMock()
    menu.setGraphicsEffect = MagicMock()
    menu.setFixedSize = MagicMock()
    menu.show = MagicMock()
    menu.hide = MagicMock()
    menu.setFocus = MagicMock()
    menu.update = MagicMock()
    menu.move = MagicMock()
    menu.width = MagicMock(return_value=200)
    menu.height = MagicMock(return_value=300)
    menu.parentWidget = MagicMock(return_value=parent)
    menu.rect = MagicMock()

    return menu, script_mgr, hotkey_mgr


# ===========================================================================
# MenuNode / MenuContext dataclass tests
# ===========================================================================

class TestMenuNode:
    def test_leaf_node(self):
        node = MenuNode("Sort by Name", key="n", script="sort_by_name")
        assert node.label == "Sort by Name"
        assert node.key == "n"
        assert node.script == "sort_by_name"
        assert node.children == []
        assert node.visible is None

    def test_branch_node(self):
        child = MenuNode("Child", key="c", script="child_script")
        parent = MenuNode("Parent", key="p", children=[child])
        assert len(parent.children) == 1
        assert parent.children[0] is child
        assert parent.script == ""

    def test_visibility_filter(self):
        node = MenuNode("Only Thumbnails", key="t", script="test",
                         visible=lambda ctx: ctx.view == "thumbnail")
        ctx_thumb = MenuContext(view="thumbnail", has_selection=False,
                                selection_count=0, file_types=set())
        ctx_picture = MenuContext(view="picture", has_selection=False,
                                  selection_count=0, file_types=set())
        assert node.visible(ctx_thumb) is True
        assert node.visible(ctx_picture) is False

    def test_default_children_not_shared(self):
        a = MenuNode("A")
        b = MenuNode("B")
        a.children.append(MenuNode("child"))
        assert b.children == []


class TestMenuContext:
    def test_fields(self):
        ctx = MenuContext(view="thumbnail", has_selection=True,
                          selection_count=3, file_types={".jpg", ".cr3"})
        assert ctx.view == "thumbnail"
        assert ctx.has_selection is True
        assert ctx.selection_count == 3
        assert ".jpg" in ctx.file_types

    def test_no_selection(self):
        ctx = MenuContext(view="picture", has_selection=False,
                          selection_count=0, file_types=set())
        assert ctx.has_selection is False
        assert ctx.selection_count == 0


# ===========================================================================
# ModalMenu tests
# ===========================================================================

class TestModalMenuOpen:
    def test_open_unknown_menu_does_nothing(self):
        menu, script_mgr, hotkey_mgr = _make_menu()
        menu.open("nonexistent")
        hotkey_mgr.disable_shortcuts.assert_not_called()
        menu.show.assert_not_called()

    def test_open_populates_visible_items(self):
        menu, _, hotkey_mgr = _make_menu()
        menu.open("test")
        assert len(menu._visible_items) == 2
        assert menu._visible_items[0].label == "Alpha"
        assert menu._visible_items[1].label == "Beta"

    def test_open_builds_key_map(self):
        menu, _, _ = _make_menu()
        menu.open("test")
        assert "a" in menu._key_map
        assert "b" in menu._key_map
        assert menu._key_map["a"].script == "do_alpha"

    def test_open_sets_breadcrumb(self):
        menu, _, _ = _make_menu()
        menu.open("test")
        assert menu._breadcrumb == ["Test Menu"]

    def test_open_disables_shortcuts(self):
        menu, _, hotkey_mgr = _make_menu()
        menu.open("test")
        hotkey_mgr.disable_shortcuts.assert_called_once()

    def test_open_shows_widget(self):
        menu, _, _ = _make_menu()
        menu.open("test")
        menu.show.assert_called_once()


class TestModalMenuKeyPress:
    def test_mapped_key_runs_script(self):
        menu, script_mgr, hotkey_mgr = _make_menu()
        menu.open("test")
        event = _make_key_event("a")
        ModalMenu.keyPressEvent(menu, event)
        script_mgr.run_script.assert_called_once_with("do_alpha")

    def test_mapped_key_closes_menu(self):
        menu, _, hotkey_mgr = _make_menu()
        menu.open("test")
        hotkey_mgr.reset_mock()
        event = _make_key_event("b")
        ModalMenu.keyPressEvent(menu, event)
        menu.hide.assert_called()
        hotkey_mgr.enable_shortcuts.assert_called()

    def test_escape_closes_menu(self):
        from PySide6.QtCore import Qt
        menu, _, hotkey_mgr = _make_menu()
        menu.open("test")
        hotkey_mgr.reset_mock()
        event = _make_key_event(qt_key=Qt.Key_Escape)
        event.text.return_value = ""
        ModalMenu.keyPressEvent(menu, event)
        menu.hide.assert_called()
        hotkey_mgr.enable_shortcuts.assert_called()

    def test_unmapped_key_closes_menu(self):
        menu, script_mgr, hotkey_mgr = _make_menu()
        menu.open("test")
        hotkey_mgr.reset_mock()
        event = _make_key_event("z")
        ModalMenu.keyPressEvent(menu, event)
        menu.hide.assert_called()
        script_mgr.run_script.assert_not_called()

    def test_case_insensitive_keys(self):
        menu, script_mgr, _ = _make_menu()
        menu.open("test")
        event = _make_key_event("A")
        event.text.return_value = "A"
        ModalMenu.keyPressEvent(menu, event)
        # .lower() in keyPressEvent should still not match — text is "A", .lower() = "a"
        # But _key_map stores lowercase keys, so "a" should match
        # event.text().lower() = "a", which is in _key_map
        script_mgr.run_script.assert_called_once_with("do_alpha")


class TestModalMenuSubMenus:
    def test_branch_node_descends(self):
        sub = MenuNode("Sub", children=[
            MenuNode("Sub Item", key="x", script="sub_action"),
        ])
        root = MenuNode("Root", children=[
            MenuNode("Go Sub", key="s", children=[sub]),
            MenuNode("Direct", key="d", script="direct_action"),
        ])
        # "Go Sub" has children, so pressing "s" should descend
        menu, script_mgr, _ = _make_menu(menus={"nav": root})
        menu.open("nav")
        assert "s" in menu._key_map
        assert menu._key_map["s"].children == [sub]

        event = _make_key_event("s")
        ModalMenu.keyPressEvent(menu, event)
        # Should NOT run a script
        script_mgr.run_script.assert_not_called()
        # Should have descended — breadcrumb grows
        assert len(menu._breadcrumb) == 2
        assert menu._breadcrumb[1] == "Go Sub"

    def test_nested_breadcrumb(self):
        leaf = MenuNode("Leaf", key="l", script="leaf_action")
        mid = MenuNode("Mid", key="m", children=[leaf])
        root = MenuNode("Top", children=[
            MenuNode("Enter", key="e", children=[mid]),
        ])
        menu, _, _ = _make_menu(menus={"deep": root})
        menu.open("deep")
        assert menu._breadcrumb == ["Top"]

        ModalMenu.keyPressEvent(menu, _make_key_event("e"))
        assert menu._breadcrumb == ["Top", "Enter"]


class TestModalMenuContextFiltering:
    def test_thumbnail_only_items_hidden_in_picture_view(self):
        menus = {
            "ctx": MenuNode("Ctx Menu", children=[
                MenuNode("Thumb Only", key="t", script="thumb_script",
                         visible=lambda ctx: ctx.view == "thumbnail"),
                MenuNode("Always", key="a", script="always_script"),
            ])
        }
        parent = _make_parent_widget(view="picture")
        menu, _, _ = _make_menu(parent=parent, menus=menus)
        menu.open("ctx")
        labels = [item.label for item in menu._visible_items]
        assert "Always" in labels
        assert "Thumb Only" not in labels

    def test_all_items_visible_in_thumbnail_view(self):
        menus = {
            "ctx": MenuNode("Ctx Menu", children=[
                MenuNode("Thumb Only", key="t", script="thumb_script",
                         visible=lambda ctx: ctx.view == "thumbnail"),
                MenuNode("Always", key="a", script="always_script"),
            ])
        }
        parent = _make_parent_widget(view="thumbnail")
        menu, _, _ = _make_menu(parent=parent, menus=menus)
        menu.open("ctx")
        labels = [item.label for item in menu._visible_items]
        assert "Thumb Only" in labels
        assert "Always" in labels

    def test_empty_menu_auto_closes(self):
        menus = {
            "empty": MenuNode("Empty", children=[
                MenuNode("Hidden", key="h", script="hidden",
                         visible=lambda ctx: False),
            ])
        }
        menu, _, hotkey_mgr = _make_menu(menus=menus)
        menu.open("empty")
        # Should have closed immediately since no items are visible
        menu.show.assert_not_called()

    def test_selection_context(self):
        parent = _make_parent_widget(
            selected_paths={"/img/a.jpg", "/img/b.cr3"})
        menu, _, _ = _make_menu(parent=parent, menus={
            "sel": MenuNode("Sel", children=[
                MenuNode("Needs Selection", key="s", script="sel_script",
                         visible=lambda ctx: ctx.has_selection),
            ])
        })
        menu.open("sel")
        assert len(menu._visible_items) == 1

    def test_file_type_context(self):
        parent = _make_parent_widget(
            selected_paths={"/img/a.jpg", "/img/b.cr3"})
        menu, _, _ = _make_menu(parent=parent, menus={
            "ft": MenuNode("FT", children=[
                MenuNode("RAW only", key="r", script="raw_script",
                         visible=lambda ctx: ".cr3" in ctx.file_types),
            ])
        })
        menu.open("ft")
        assert len(menu._visible_items) == 1
        assert menu._context.file_types == {".jpg", ".cr3"}

    def test_hovered_fallback_for_file_types(self):
        parent = _make_parent_widget(
            selected_paths=set(), hovered="/photos/sunset.png")
        menu, _, _ = _make_menu(parent=parent, menus={
            "hover": MenuNode("Hover", children=[
                MenuNode("PNG", key="p", script="png_script",
                         visible=lambda ctx: ".png" in ctx.file_types),
            ])
        })
        menu.open("hover")
        assert menu._context.file_types == {".png"}
        assert len(menu._visible_items) == 1


class TestBuildContextViewDetection:
    def test_thumbnail_view(self):
        parent = _make_parent_widget(view="thumbnail")
        menu, _, _ = _make_menu(parent=parent)
        ctx = ModalMenu._build_context(menu)
        assert ctx.view == "thumbnail"

    def test_picture_view(self):
        parent = _make_parent_widget(view="picture")
        menu, _, _ = _make_menu(parent=parent)
        ctx = ModalMenu._build_context(menu)
        assert ctx.view == "picture"

    def test_video_view(self):
        parent = _make_parent_widget(view="video")
        menu, _, _ = _make_menu(parent=parent)
        ctx = ModalMenu._build_context(menu)
        assert ctx.view == "video"


# ===========================================================================
# menu_registry tests
# ===========================================================================

class TestMenuRegistry:
    def test_build_menus_returns_sort(self):
        menus = build_menus()
        assert "sort" in menus

    def test_sort_menu_has_expected_items(self):
        menus = build_menus()
        sort_root = menus["sort"]
        assert sort_root.label == "Sort"
        keys = {child.key for child in sort_root.children}
        assert keys == {"d", "n", "r", "s", "t"}

    def test_sort_items_have_scripts(self):
        menus = build_menus()
        for child in menus["sort"].children:
            assert child.script.startswith("sort_by_")

    def test_sort_items_visible_in_thumbnail(self):
        ctx = MenuContext(view="thumbnail", has_selection=False,
                          selection_count=0, file_types=set())
        menus = build_menus()
        for child in menus["sort"].children:
            assert child.visible(ctx) is True

    def test_sort_items_hidden_in_picture_view(self):
        ctx = MenuContext(view="picture", has_selection=False,
                          selection_count=0, file_types=set())
        menus = build_menus()
        for child in menus["sort"].children:
            assert child.visible(ctx) is False

    def test_sort_scripts_match_keys(self):
        menus = build_menus()
        expected = {
            "d": "sort_by_date",
            "n": "sort_by_name",
            "r": "sort_by_rating",
            "s": "sort_by_size",
            "t": "sort_by_type",
        }
        for child in menus["sort"].children:
            assert child.script == expected[child.key]


# ===========================================================================
# HotkeyManager disable/enable tests
# ===========================================================================

def _ensure_hotkey_stubs():
    """Add stubs needed to import gui.hotkey_manager."""
    qtgui = sys.modules.get("PySide6.QtGui")
    for name in ("QKeySequence", "QShortcut", "QKeyEvent", "QKeyCombination"):
        if not hasattr(qtgui, name):
            setattr(qtgui, name, type(name, (), {"__init__": lambda self, *a, **kw: None}))

    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    if not hasattr(qtwidgets, "QApplication"):
        class _QApplication:
            @staticmethod
            def instance(): return None
        qtwidgets.QApplication = _QApplication

    qtcore = sys.modules["PySide6.QtCore"]
    if not hasattr(qtcore, "QKeyCombination"):
        qtcore.QKeyCombination = type("QKeyCombination", (), {"__init__": lambda self, *a: None})
    for attr in ("ApplicationShortcut", "Key_Shift"):
        if not hasattr(qtcore.Qt, attr):
            setattr(qtcore.Qt, attr, 0)


class TestHotkeyManagerMenuIntegration:
    def test_disable_enable_shortcuts(self):
        _ensure_hotkey_stubs()
        from gui.hotkey_manager import HotkeyManager

        hm = object.__new__(HotkeyManager)
        mock_shortcut_1 = MagicMock()
        mock_shortcut_2 = MagicMock()
        hm.shortcuts = {
            "action_a": [mock_shortcut_1],
            "action_b": [mock_shortcut_2],
        }

        hm.disable_shortcuts()
        mock_shortcut_1.setEnabled.assert_called_with(False)
        mock_shortcut_2.setEnabled.assert_called_with(False)

        mock_shortcut_1.reset_mock()
        mock_shortcut_2.reset_mock()

        hm.enable_shortcuts()
        mock_shortcut_1.setEnabled.assert_called_with(True)
        mock_shortcut_2.setEnabled.assert_called_with(True)

    def test_menu_prefix_registered(self):
        _ensure_hotkey_stubs()
        from gui.hotkey_manager import HotkeyManager

        parent = MagicMock()
        parent.modal_menu = MagicMock()

        hm = object.__new__(HotkeyManager)
        hm.shortcuts = {}
        hm.definitions = {}
        hm.actions = {}
        hm.parent_widget = parent

        hm.load_config({
            "menu:sort": {
                "sequence": "F",
                "description": "Open sort menu",
            }
        })

        assert "menu:sort" in hm.actions
        hm.actions["menu:sort"]()
        parent.modal_menu.open.assert_called_once_with("sort")
