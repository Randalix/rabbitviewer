"""Static analysis: every filesystem I/O call in production code must be
marked with an inline ``# disk-io: <reason>`` comment.

When this test fails it means someone added a new ``os.stat``, ``os.path.exists``,
``open()``, ``Image.open(<file>)``, etc. without acknowledging it.  That's not
necessarily wrong — but it forces a conscious decision:

1. Is this hitting a **NAS source file** or a **local cache file**?
2. Can the result be reused from an earlier stage instead of re-reading?
3. Add ``# disk-io: <reason>`` on the same line explaining *why* the I/O is
   necessary.

Run with ``pytest tests/test_disk_io_allowlist.py -v``.
"""
import ast
from pathlib import Path
from typing import Set

# ── Production source directories to scan ──────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DIRS = ["core", "plugins", "filewatcher", "gui", "config", "scripts",
                "cli", "utils"]

_MARKER = "disk-io"

# ── I/O patterns we track ──────────────────────────────────────────────────
# We detect:
#   os.stat(...)            os.path.exists(...)     os.path.isfile(...)
#   os.path.isdir(...)      os.path.getsize(...)    os.path.getmtime(...)
#   os.listdir(...)         os.scandir(...)         os.walk(...)
#   os.access(...)          open(...)               Image.open(<file>)
#   Path.read_text(...)     Path.read_bytes(...)

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

    @staticmethod
    def _is_bytesio_arg(node: ast.expr) -> bool:
        """True if *node* is ``io.BytesIO(...)``."""
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        # io.BytesIO(...)
        if (isinstance(func, ast.Attribute)
                and func.attr == "BytesIO"
                and isinstance(func.value, ast.Name)
                and func.value.id == "io"):
            return True
        # BytesIO(...)  (bare import)
        if isinstance(func, ast.Name) and func.id == "BytesIO":
            return True
        return False

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

        # Image.open(<file>) — but NOT Image.open(io.BytesIO(...))
        if (isinstance(func, ast.Attribute)
                and func.attr == "open"
                and isinstance(func.value, ast.Name)
                and func.value.id == "Image"
                and node.args
                and not self._is_bytesio_arg(node.args[0])):
            return True

        # Path(...).read_text() / .read_bytes()
        if (isinstance(func, ast.Attribute)
                and func.attr in ("read_text", "read_bytes")):
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


def _has_marker(filepath: Path, lineno: int) -> bool:
    """Check whether *lineno* in *filepath* contains the disk-io marker."""
    lines = filepath.read_text(encoding="utf-8").splitlines()
    if lineno < 1 or lineno > len(lines):
        return False
    return _MARKER in lines[lineno - 1]


# ── The test ───────────────────────────────────────────────────────────────

def test_no_unregistered_disk_io():
    """Every filesystem I/O call in production code must have a ``# disk-io`` marker.

    If this test fails, you added a new disk read.  Ask yourself:
    - Is this on a NAS source file or a local cache file?
    - Can the caller pass a cached stat result instead?
    - Is this a guard that a caller already performed?
    Then add ``# disk-io: <reason>`` on that line.
    """
    unmarked: list[str] = []
    stale: list[str] = []

    for src_dir in _SOURCE_DIRS:
        dir_path = _PROJECT_ROOT / src_dir
        if not dir_path.is_dir():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            if py_file.name.startswith("__"):
                continue
            rel = str(py_file.relative_to(_PROJECT_ROOT))
            source_lines = py_file.read_text(encoding="utf-8").splitlines()
            hits = _scan_file(py_file)

            for line in sorted(hits):
                if not _has_marker(py_file, line):
                    # Show the offending source line for easy diagnosis.
                    src = source_lines[line - 1].strip() if line <= len(source_lines) else "?"
                    unmarked.append(f"  {rel}:{line}  {src}")

            # Detect stale markers: lines with # disk-io that have no I/O call.
            for idx, text in enumerate(source_lines, 1):
                if _MARKER in text and idx not in hits:
                    stale.append(f"  {rel}:{idx}")

    msg_parts = []
    if unmarked:
        msg_parts.append(
            "Unmarked filesystem I/O calls (add '# disk-io: <reason>' on the line):\n"
            + "\n".join(unmarked)
        )
    if stale:
        msg_parts.append(
            "Stale disk-io markers (I/O call was removed — delete the marker):\n"
            + "\n".join(stale)
        )

    assert not msg_parts, "\n\n".join(msg_parts)
