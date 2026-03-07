"""Static analysis: every filesystem I/O call in production code must be
registered in the allowlist below.

When this test fails it means someone added a new ``os.stat``, ``os.path.exists``,
``open()``, etc.  That's not necessarily wrong — but it forces a conscious
decision:

1. Is this hitting a **NAS source file** or a **local cache file**?
2. Can the result be reused from an earlier stage instead of re-reading?
3. Add the call to the allowlist with a comment explaining *why* the I/O is
   necessary.

Run with ``pytest tests/test_disk_io_allowlist.py -v``.
"""
import ast
from pathlib import Path
from typing import Dict, Set

# ── Production source directories to scan ──────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DIRS = ["core", "plugins", "filewatcher", "gui", "config", "scripts",
                "cli", "utils"]

# ── I/O patterns we track ──────────────────────────────────────────────────
# Each pattern is (object, attribute) for method calls, or just a function name.
# We detect:
#   os.stat(...)            os.path.exists(...)     os.path.isfile(...)
#   os.path.isdir(...)      os.path.getsize(...)    os.path.getmtime(...)
#   os.listdir(...)         os.scandir(...)         os.walk(...)
#   os.access(...)          open(...)
#
# The AST visitor reports each as "file:line:call" so the allowlist is precise.

# Allowlist format: { "relative/path.py": {line_number, ...} }
# A call site is allowed iff its file+line appears here.
#
# To regenerate after intentional changes:
#   pytest tests/test_disk_io_allowlist.py -v --tb=long
# Copy the "Unregistered" lines from the failure message into this dict.

ALLOWLIST: Dict[str, Set[int]] = {
    # ── core/directory_scanner.py ──────────────────────────────────────
    # os.path.isdir: guard before os.walk
    # os.walk/os.listdir: main directory discovery
    # os.stat: is_supported_file single-stat check
    "core/directory_scanner.py": {63, 90, 96},

    # ── core/source_cache.py ────────────────────────────────────────
    # os.path.exists / os.stat: cached existence + stat lookups
    "core/source_cache.py": {26, 40},

    # ── core/thumbnail_manager.py ──────────────────────────────────────
    # source_cache.exists: worker task entry guards (deduplicated via TTL cache)
    # source_cache.stat: _passes_pre_checks (single stat, cached)
    # os.path.exists: sync get_thumbnail, cache file checks, write guards
    # os.path.getsize: cache size tracking
    # os.stat: volume accessibility probe
    # open: header reads, mem-cache reads
    "core/thumbnail_manager.py": {
        130, 138, 198, 262, 322, 551, 613, 649, 672, 688,
        751, 783, 809, 830, 870, 903, 990, 1029, 1083,
    },

    # ── core/metadata_database.py ──────────────────────────────────────
    # os.stat: freshness hashing, metadata extraction, thumbnail validity
    # os.path.exists: cache file checks, sidecar checks, ghost cleanup
    # os.path.getsize: cache size accounting
    "core/metadata_database.py": {
        173, 271, 311, 358, 393, 422, 520, 557, 665, 687, 714, 746,
        763, 785, 902, 986, 1167, 1243, 1511, 1538,
    },

    # ── core/thumbnail_service.py ──────────────────────────────────────
    # os.path.exists: cache file checks in get_previews_status
    "core/thumbnail_service.py": {181, 183},

    # ── core/priority.py ──────────────────────────────────────────────
    # os.path.exists: XMP sidecar discovery in ImageEntry.from_path
    "core/priority.py": {128},

    # ── core/resource_lock.py ─────────────────────────────────────────
    # os.path.exists / open: lock directory and lock file
    "core/resource_lock.py": {29, 59, 62},

    # ── core/background_indexer.py ────────────────────────────────────
    # os.path.exists: watch path validation at startup
    "core/background_indexer.py": {81},

    # ── core/comfyui_client.py ────────────────────────────────────────
    # Image.open / Path.read_bytes: image upload preparation
    "core/comfyui_client.py": {116, 121},

    # ── core/file_ops.py ──────────────────────────────────────────────
    # os.path.exists: sidecar resolution
    "core/file_ops.py": {19},

    # ── plugins/base_plugin.py ────────────────────────────────────────
    # os.path.exists: sidecar checks
    # os.listdir: plugin directory discovery
    # open: header scan, XMP sidecar read
    "plugins/base_plugin.py": {30, 86, 208, 233, 235, 304, 345},

    # ── plugins/cr3_plugin.py ─────────────────────────────────────────
    # open: header read for orientation
    # Image.open: JPEG verify, view image processing
    # os.path.exists: cache file checks
    "plugins/cr3_plugin.py": {35, 105, 173, 192, 220, 253, 258, 273},

    # ── plugins/raw_plugin.py ─────────────────────────────────────────
    # open: header read for orientation
    # Image.open: view image / thumbnail processing
    # os.path.exists: cache file checks
    "plugins/raw_plugin.py": {53, 95, 109, 123, 137, 140, 151},

    # ── plugins/pil_plugin.py ─────────────────────────────────────────
    # Image.open: thumbnail and view image generation from source
    # os.path.exists: cache file checks
    "plugins/pil_plugin.py": {31, 55, 80, 90},

    # ── plugins/video_plugin.py ───────────────────────────────────────
    # os.path.exists: cache file checks after ffmpeg
    "plugins/video_plugin.py": {41, 61, 69, 87},

    # ── plugins/exiftool_process.py ───────────────────────────────────
    # os.path.isfile / os.access: binary discovery
    "plugins/exiftool_process.py": {47},

    # ── filewatcher/watcher.py ────────────────────────────────────────
    # os.path.exists: watch path validation, XMP orphan cleanup
    # os.path.isdir: GUI directory validation
    "filewatcher/watcher.py": {51, 91, 208},

    # ── gui/main_window.py ────────────────────────────────────────────
    # os.path.isdir / os.path.isfile: drop/open path classification
    "gui/main_window.py": {595, 597, 601},

    # ── gui/hotkey_manager.py ─────────────────────────────────────────
    # open: keybindings.json load
    "gui/hotkey_manager.py": {86},

    # ── gui/comfyui_dialog.py ─────────────────────────────────────────
    # os.listdir: workflow directory listing
    # open: workflow JSON load
    "gui/comfyui_dialog.py": {176, 214},

    # ── config/config_manager.py ──────────────────────────────────────
    # open: YAML config load (two call sites)
    "config/config_manager.py": {177, 188},

    # ── scripts/script_manager.py ─────────────────────────────────────
    # os.path.exists / os.listdir: script directory discovery
    "scripts/script_manager.py": {24, 28},

    # ── scripts/sort_by_date.py ───────────────────────────────────────
    # os.path.getmtime: sort key
    "scripts/sort_by_date.py": {18},

    # ── cli/stop.py ───────────────────────────────────────────────────
    # open: PID file read (two call sites)
    "cli/stop.py": {26, 40},

    # ── cli/rabbit.py ─────────────────────────────────────────────────
    # Path.read_text: script source loading
    "cli/rabbit.py": {35, 53},
}


# ── AST visitor ────────────────────────────────────────────────────────────

# Functions/methods that perform filesystem I/O.
_OS_PATH_CALLS = {"exists", "isfile", "isdir", "getsize", "getmtime", "getatime"}
_OS_CALLS = {"stat", "listdir", "scandir", "walk", "access", "fstat"}


class _IOCallVisitor(ast.NodeVisitor):
    """Collects line numbers of filesystem I/O calls in a single file."""

    def __init__(self):
        self.hits: Set[int] = set()

    def visit_Call(self, node: ast.Call):
        if self._is_io_call(node):
            self.hits.add(node.lineno)
        self.generic_visit(node)

    def _is_io_call(self, node: ast.Call) -> bool:
        func = node.func

        # open(...)
        if isinstance(func, ast.Name) and func.id == "open":
            return True

        # os.stat(...), os.walk(...), etc.
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr in _OS_CALLS):
            return True

        # os.path.exists(...), os.path.isfile(...), etc.
        if (isinstance(func, ast.Attribute)
                and func.attr in _OS_PATH_CALLS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"):
            return True

        # Path(...).read_text() / .read_bytes() / .open() / .stat()
        if (isinstance(func, ast.Attribute)
                and func.attr in ("read_text", "read_bytes", "open", "stat")):
            return True

        return False


def _scan_file(filepath: Path) -> Set[int]:
    """Return line numbers of filesystem I/O calls in *filepath*."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return set()
    visitor = _IOCallVisitor()
    visitor.visit(tree)
    return visitor.hits


# ── The test ───────────────────────────────────────────────────────────────

def test_no_unregistered_disk_io():
    """Every filesystem I/O call in production code must be in the allowlist.

    If this test fails, you added a new disk read.  Ask yourself:
    - Is this on a NAS source file or a local cache file?
    - Can the caller pass a cached stat result instead?
    - Is this a guard that a caller already performed?
    Then add the call to ALLOWLIST with a comment explaining why.
    """
    unregistered: list[str] = []
    stale: list[str] = []

    for src_dir in _SOURCE_DIRS:
        dir_path = _PROJECT_ROOT / src_dir
        if not dir_path.is_dir():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            if py_file.name.startswith("__"):
                continue
            rel = str(py_file.relative_to(_PROJECT_ROOT))
            hits = _scan_file(py_file)
            allowed = ALLOWLIST.get(rel, set())

            for line in sorted(hits - allowed):
                unregistered.append(f"  {rel}:{line}")
            for line in sorted(allowed - hits):
                stale.append(f"  {rel}:{line}")

    msg_parts = []
    if unregistered:
        msg_parts.append(
            "Unregistered filesystem I/O calls (add to ALLOWLIST or eliminate):\n"
            + "\n".join(unregistered)
        )
    if stale:
        msg_parts.append(
            "Stale allowlist entries (call was removed — clean up ALLOWLIST):\n"
            + "\n".join(stale)
        )

    assert not msg_parts, "\n\n".join(msg_parts)
