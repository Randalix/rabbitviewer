"""Verify every registered hotkey action has a default config entry."""
import ast
from config.config_manager import DEFAULT_CONFIG


def _extract_add_action_names(*filepaths):
    """Parse add_action("name", ...) calls from source files via AST."""
    names = set()
    for path in filepaths:
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_action"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


# Actions that intentionally have no standalone keybinding because they are
# reachable through the modal menu system (e.g. T -> A for tag editor).
MENU_ONLY_ACTIONS = {"open_tag_editor"}


def test_all_actions_have_default_config():
    actions = _extract_add_action_names(
        "gui/hotkey_manager.py",
        "gui/main_window.py",
    )
    default_hotkeys = DEFAULT_CONFIG["hotkeys"]

    # script: and menu: actions get their config entries dynamically
    actions = {a for a in actions if not a.startswith("script:") and not a.startswith("menu:")}
    actions -= MENU_ONLY_ACTIONS

    missing = actions - set(default_hotkeys.keys())
    assert not missing, (
        f"Actions registered via add_action() but missing from "
        f"DEFAULT_CONFIG['hotkeys']: {sorted(missing)}"
    )


def test_default_hotkeys_have_sequence():
    for name, entry in DEFAULT_CONFIG["hotkeys"].items():
        assert isinstance(entry, dict), f"hotkey '{name}' should be a dict"
        assert "sequence" in entry, f"hotkey '{name}' missing 'sequence' key"
        assert entry["sequence"], f"hotkey '{name}' has empty sequence"


def test_no_duplicate_sequences():
    """Every key sequence in DEFAULT_CONFIG must map to exactly one action."""
    seq_to_actions: dict[str, list[str]] = {}
    for name, entry in DEFAULT_CONFIG["hotkeys"].items():
        if not isinstance(entry, dict):
            continue
        sequences = []
        if "sequence" in entry:
            sequences.append(entry["sequence"])
        if "extra_sequences" in entry:
            sequences.extend(entry["extra_sequences"])
        for seq in sequences:
            normalised = seq.strip()
            seq_to_actions.setdefault(normalised, []).append(name)

    dupes = {seq: actions for seq, actions in seq_to_actions.items() if len(actions) > 1}
    assert not dupes, (
        f"Duplicate key sequences in DEFAULT_CONFIG['hotkeys']: "
        + ", ".join(f"'{seq}' -> {actions}" for seq, actions in sorted(dupes.items()))
    )


def test_default_config_covers_all_actions():
    """Every non-script/menu action in DEFAULT_CONFIG should have a handler registered."""
    actions = _extract_add_action_names(
        "gui/hotkey_manager.py",
        "gui/main_window.py",
    )
    default_hotkeys = set(DEFAULT_CONFIG["hotkeys"].keys())
    # Config entries that don't need a code-side add_action (scripts/menus are dynamic)
    config_only = {k for k in default_hotkeys if k.startswith("script:") or k.startswith("menu:")}
    orphaned = (default_hotkeys - config_only) - actions
    assert not orphaned, (
        f"Hotkey config entries with no registered add_action() handler: {sorted(orphaned)}"
    )
