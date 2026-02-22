#!/usr/bin/env bash
# install.sh — install RabbitViewer CLI tools so they are available from any directory.
#
# Usage:
#   ./install.sh             # install / reinstall
#   ./install.sh --update    # pull latest changes, then reinstall
#   ./install.sh --clean     # wipe the venv and reinstall from scratch
#   ./install.sh --uninstall # remove CLI wrappers from ~/.local/bin
#
# What it does:
#   1. Creates (or reuses) a venv at <repo>/venv using Python 3.10–3.13
#      (PySide6 does not yet support Python 3.14+).
#   2. Installs the package in editable mode (source changes take effect immediately).
#   3. Writes thin wrapper scripts into ~/.local/bin/ that prepend the repo to
#      PYTHONPATH before invoking the venv entry point.  This guarantees the
#      project's own packages (e.g. utils/) win over same-named modules that may
#      exist elsewhere on the user's PYTHONPATH.
#      Note: wrappers embed the absolute path to the venv; moving the repo requires
#      re-running install.sh.
#   4. Ensures ~/.local/bin is in PATH, patching the user's shell rc if needed.
#
# Requirements: Python 3.10–3.13

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/venv"
BIN_DIR="$HOME/.local/bin"
SCRIPTS=("rabbitviewer" "rabbitviewer-daemon")
CLI_DIR="$REPO_DIR/cli"    # used by rabbit dispatcher wrapper
MODE="${1:-}"

# ── argument validation ────────────────────────────────────────────────────────

case "$MODE" in
    ""|--clean|--uninstall|--update) ;;
    *) printf 'Unknown argument: %s\nUsage: %s [--clean|--uninstall|--update]\n' "$MODE" "$0" >&2; exit 1 ;;
esac

# ── helpers ───────────────────────────────────────────────────────────────────

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n'  "$*"; }

# Find a Python 3.10–3.13 interpreter.
# PySide6 6.x requires Python < 3.14, so we try specific minor versions first,
# then fall back to the generic python3 only if it is in the supported range.
require_python() {
    local ok
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" &>/dev/null; then
            ok=$(python_version_ok "$candidate")
            if [[ "$ok" == "ok" ]]; then
                echo "$candidate"
                return
            fi
        fi
    done
    red "No compatible Python found (need 3.10–3.13; PySide6 does not support 3.14+)."
    exit 1
}

# Check whether a given Python executable is in the supported version range.
python_version_ok() {
    "$1" -c "
import sys
v = sys.version_info
print('ok' if (3,10) <= v < (3,14) else 'no')
" 2>/dev/null || echo no
}

# Returns the path of the user's shell rc file, or empty string if unknown.
detect_shell_rc() {
    local shell_name
    shell_name="$(basename "${SHELL:-}")"
    case "$shell_name" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash)
            if [[ "$(uname)" == "Darwin" ]]; then
                echo "$HOME/.bash_profile"
            else
                echo "$HOME/.bashrc"
            fi
            ;;
        fish) echo "$HOME/.config/fish/config.fish" ;;
        *)    echo "" ;;
    esac
}

# Map a script name to its venv entry point path.
entry_point_for() {
    case "$1" in
        rabbitviewer)        echo "$VENV_DIR/bin/rabbitviewer" ;;
        rabbitviewer-daemon) echo "$VENV_DIR/bin/rabbitviewer-daemon" ;;
        *) red "Unknown script: $1"; exit 1 ;;
    esac
}

# ── uninstall ─────────────────────────────────────────────────────────────────

if [[ "$MODE" == "--uninstall" ]]; then
    yellow "Removing wrappers from $BIN_DIR …"
    for script in "${SCRIPTS[@]}"; do
        target="$BIN_DIR/$script"
        if [[ -f "$target" || -L "$target" ]]; then
            rm "$target"
            green "  removed $target"
        else
            yellow "  $target not found, skipping"
        fi
    done
    # Remove rabbit CLI dispatcher.
    target="$BIN_DIR/rabbit"
    if [[ -f "$target" || -L "$target" ]]; then
        rm "$target"
        green "  removed $target"
    else
        yellow "  $target not found, skipping"
    fi
    # Remove shell completions.
    for comp_file in \
        "$HOME/.local/share/bash-completion/completions/rabbit" \
        "$HOME/.local/share/zsh/site-functions/_rabbit"; do
        if [[ -f "$comp_file" ]]; then
            rm "$comp_file"
            green "  removed $comp_file"
        fi
    done
    yellow "Done. The venv at $VENV_DIR was left intact."
    yellow "Note: any PATH entry added to your shell rc file was not removed."
    exit 0
fi

# ── clean flag ────────────────────────────────────────────────────────────────

# When --clean is requested: record the venv's Python path before wiping so we
# can recreate the venv with the same interpreter.
SAVED_PYTHON=""
if [[ "$MODE" == "--clean" ]]; then
    if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
        # Resolve to the real system executable before the venv is deleted.
        # The venv's python is a symlink; we follow it so the path survives rm -rf.
        SAVED_PYTHON="$(python_real="$VENV_DIR/bin/python"
            if command -v realpath &>/dev/null; then
                realpath "$python_real"
            else
                # macOS fallback: readlink -f may not exist; use Python itself
                "$python_real" -c "import os,sys; print(os.path.realpath(sys.executable))"
            fi)"
        yellow "Remembered venv Python: $SAVED_PYTHON ($("$SAVED_PYTHON" --version))"
    fi
    if [[ -d "$VENV_DIR" ]]; then
        yellow "Removing existing venv at $VENV_DIR …"
        rm -rf "$VENV_DIR"
    fi
    rm -rf "$REPO_DIR"/rabbitviewer.egg-info
fi

# ── optional update ───────────────────────────────────────────────────────────

if [[ "$MODE" == "--update" ]]; then
    yellow "Pulling latest changes …"
    if git -C "$REPO_DIR" pull --ff-only 2>&1; then
        green "Repository up to date."
    else
        yellow "Could not fast-forward (local changes or detached HEAD) — skipping pull."
    fi
fi

# ── install ───────────────────────────────────────────────────────────────────

# 1. Choose the Python interpreter.
#    Priority: venv's own Python (for --clean rebuilds) > system search.
#    Re-validate the saved path in case a system upgrade pushed it out of range.
if [[ -n "$SAVED_PYTHON" && -x "$SAVED_PYTHON" && "$(python_version_ok "$SAVED_PYTHON")" == "ok" ]]; then
    PYTHON="$SAVED_PYTHON"
    green "Using saved venv Python: $("$PYTHON" --version)"
else
    PYTHON=$(require_python)
    green "Using Python: $("$PYTHON" --version)"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# 2. Validate or create the venv.
if [[ -d "$VENV_DIR" ]]; then
    if "$VENV_PYTHON" -c "import sys" &>/dev/null; then
        yellow "Reusing existing venv at $VENV_DIR"
    else
        yellow "Existing venv is broken — recreating …"
        rm -rf "$VENV_DIR" "$REPO_DIR"/rabbitviewer.egg-info
        "$PYTHON" -m venv "$VENV_DIR"
    fi
else
    yellow "Creating venv at $VENV_DIR …"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# 3. Check for exiftool.
if command -v exiftool &>/dev/null; then
    green "exiftool found: $(exiftool -ver)"
else
    yellow "exiftool not found — RAW support and rating write-back will not work."
    if [[ "$(uname)" == "Darwin" ]]; then
        yellow "  Install with: brew install exiftool"
    else
        yellow "  Install with: sudo apt install libimage-exiftool-perl"
    fi
fi

# 4. Editable install.
yellow "Installing RabbitViewer (editable) …"
"$VENV_PIP" install --upgrade pip
"$VENV_PIP" install -e "$REPO_DIR"
green "Package installed."

# 5. Write wrapper scripts into ~/.local/bin.
#    Wrappers embed the absolute venv path set at install time; re-run install.sh
#    if the repo is moved.
#    rm -f before writing: prevents cat > dst from following a leftover symlink
#    and overwriting the pip-generated entry point inside the venv.
mkdir -p "$BIN_DIR"

for script in "${SCRIPTS[@]}"; do
    src="$(entry_point_for "$script")"
    dst="$BIN_DIR/$script"

    if [[ ! -f "$src" ]]; then
        red "Entry point not found after install: $src"
        exit 1
    fi

    rm -f "$dst"
    cat > "$dst" <<WRAPPER
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec "${src}" "\$@"
WRAPPER
    chmod +x "$dst"
    green "  $dst → $src"
done

# 5b. Write wrapper for `rabbit` CLI dispatcher.
RABBIT_SRC="$CLI_DIR/rabbit.py"
RABBIT_DST="$BIN_DIR/rabbit"
rm -f "$RABBIT_DST"
cat > "$RABBIT_DST" <<WRAPPER
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec "${VENV_PYTHON}" "${RABBIT_SRC}" "\$@"
WRAPPER
chmod +x "$RABBIT_DST"
green "  $RABBIT_DST → $RABBIT_SRC"

# 5c. Install shell completions for `rabbit`.
install_bash_completion() {
    local dir="$HOME/.local/share/bash-completion/completions"
    mkdir -p "$dir"
    cat > "$dir/rabbit" <<'COMP'
_rabbit() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    if [[ "$COMP_CWORD" -eq 1 ]]; then
        local cmds
        cmds="$(rabbit --complete 2>/dev/null)"
        COMPREPLY=($(compgen -W "$cmds" -- "$cur"))
    fi
}
complete -F _rabbit rabbit
COMP
    green "  bash completions → $dir/rabbit"
}

install_zsh_completion() {
    local dir="$HOME/.local/share/zsh/site-functions"
    mkdir -p "$dir"
    cat > "$dir/_rabbit" <<'COMP'
#compdef rabbit
_rabbit() {
    local -a commands
    if (( CURRENT == 2 )); then
        commands=("${(@f)$(rabbit --complete 2>/dev/null)}")
        _describe 'command' commands
    else
        _files
    fi
}
_rabbit "$@"
COMP
    green "  zsh completions  → $dir/_rabbit"
}

install_bash_completion
install_zsh_completion

# Ensure zsh picks up the custom completions directory.
ZSH_FPATH_DIR="$HOME/.local/share/zsh/site-functions"
RC_FILE_ZSH="$HOME/.zshrc"
if [[ -f "$RC_FILE_ZSH" ]]; then
    if ! grep -qF "$ZSH_FPATH_DIR" "$RC_FILE_ZSH" 2>/dev/null; then
        printf '\n# Added by RabbitViewer install.sh — rabbit completions\nfpath=(%s $fpath)\nautoload -Uz compinit && compinit\n' "$ZSH_FPATH_DIR" >> "$RC_FILE_ZSH"
        green "  Added fpath entry to $RC_FILE_ZSH"
    fi
fi

# 6. Ensure ~/.local/bin is in PATH.
echo
shell_name="$(basename "${SHELL:-}")"
if [[ "$shell_name" == "fish" ]]; then
    PATH_LINE='fish_add_path $HOME/.local/bin'
else
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
fi

if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
    green "$BIN_DIR is in PATH."
else
    yellow "$BIN_DIR is not in your current PATH."

    RC_FILE="$(detect_shell_rc)"
    if [[ -n "$RC_FILE" ]]; then
        if grep -qF "$HOME/.local/bin" "$RC_FILE" 2>/dev/null; then
            yellow "  PATH entry found in $RC_FILE but not active in this shell."
            yellow "  Run: source $RC_FILE"
        else
            yellow "  Adding PATH entry to $RC_FILE …"
            printf '\n# Added by RabbitViewer install.sh\n%s\n' "$PATH_LINE" >> "$RC_FILE"
            green "  Done. Open a new terminal or run: source $RC_FILE"
        fi
    else
        yellow "  Could not detect shell config file."
        yellow "  Add this line to your shell's rc file manually:"
        yellow ""
        yellow "    $PATH_LINE"
    fi
fi

echo
bold "Installation complete."
green "Run: rabbitviewer [directory]"
green "CLI: rabbit --help"
