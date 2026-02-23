#!/usr/bin/env python3
"""Dispatch CLI for RabbitViewer.

Discovers subcommands from sibling .py files in the cli/ directory.
File names are converted to subcommand names by replacing underscores
with hyphens (e.g. move_selected.py → move-selected).
"""

import importlib.util
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve().name


def _discover_commands() -> dict[str, Path]:
    """Return {subcommand-name: path} for every .py file in cli/."""
    cmds: dict[str, Path] = {}
    for p in sorted(CLI_DIR.glob("*.py")):
        if p.name.startswith("_") or p.name == _SELF:
            continue
        name = p.stem.replace("_", "-")
        cmds[name] = p
    return cmds


def _print_usage(commands: dict[str, Path]) -> None:
    print("usage: rabbit <command> [args ...]\n")
    print("Available commands:")
    for name, path in commands.items():
        # Grab the module docstring's first line as a description.
        desc = ""
        try:
            src = path.read_text()
            mod = compile(src, str(path), "exec")
            if isinstance(mod.co_consts[0], str):
                desc = mod.co_consts[0].strip().split("\n")[0]
        except Exception:
            pass
        print(f"  {name:24s} {desc}")
    print()


def _extract_flags(path: Path) -> list[str]:
    """Return completions for a subcommand.

    If the source defines ``__completions__ = [...]`` use that list;
    otherwise fall back to scanning for ``--flag`` strings.
    """
    import ast, re
    try:
        src = path.read_text()
    except OSError:
        return []
    # Look for a module-level __completions__ list.
    try:
        tree = ast.parse(src)
        for node in ast.iter_child_nodes(tree):
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__completions__"):
                val = ast.literal_eval(node.value)
                if isinstance(val, list):
                    return val
    except Exception:
        pass
    return sorted(set(re.findall(r'--[a-z][-a-z0-9]*', src)))


def _complete() -> None:
    """Print completion candidates and exit.

    ``rabbit --complete``            → subcommand names + --help
    ``rabbit --complete <subcmd>``   → flags for that subcommand
    """
    commands = _discover_commands()

    # If a subcommand was given, emit its flags.
    if len(sys.argv) >= 3:
        subcmd = sys.argv[2].replace("_", "-")
        if subcmd in commands:
            flags = _extract_flags(commands[subcmd])
            if flags:
                print("\n".join(flags))
        return

    top_level = list(commands.keys()) + ["--help"]
    print("\n".join(top_level))


def main() -> None:
    commands = _discover_commands()

    if len(sys.argv) >= 2 and sys.argv[1] == "--complete":
        _complete()
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        _print_usage(commands)
        sys.exit(0)

    # Resolve subcommand.  If the first arg is a known command use it;
    # otherwise default to "viewer" and pass all args through (e.g.
    # `rabbit /some/dir` becomes `rabbit viewer /some/dir`).
    if len(sys.argv) >= 2:
        normalised = sys.argv[1].replace("_", "-")
        if normalised in commands:
            cmd = normalised
            sub_argv = sys.argv[2:]
        else:
            cmd = "viewer"
            sub_argv = sys.argv[1:]
    else:
        cmd = "viewer"
        sub_argv = []

    script = commands[cmd]
    sys.argv = [str(script)] + sub_argv

    # Import and run the module's main() if it has one, otherwise exec.
    spec = importlib.util.spec_from_file_location("__main__", script)
    if spec is None or spec.loader is None:
        print(f"rabbit: failed to load '{cmd}'", file=sys.stderr)
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "__main__"
    spec.loader.exec_module(mod)


if __name__ == "__main__":
    main()
