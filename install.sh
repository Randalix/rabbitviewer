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
CLI_DIR="$REPO_DIR/cli"
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

# ── uninstall ─────────────────────────────────────────────────────────────────

if [[ "$MODE" == "--uninstall" ]]; then
    yellow "Removing wrappers from $BIN_DIR …"
    # Remove legacy wrappers from previous installs.
    for legacy in rabbitviewer rabbitviewer-daemon; do
        target="$BIN_DIR/$legacy"
        if [[ -f "$target" || -L "$target" ]]; then
            rm "$target"
            green "  removed $target (legacy)"
        fi
    done
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
    # Remove desktop entry and icon (Linux only).
    for desktop_file in \
        "$HOME/.local/share/applications/rabbitviewer.desktop" \
        "$HOME/.local/share/icons/hicolor/256x256/apps/rabbitviewer.png"; do
        if [[ -f "$desktop_file" ]]; then
            rm "$desktop_file"
            green "  removed $desktop_file"
        fi
    done
    # Remove .app bundle (macOS only).
    APP_BUNDLE="$HOME/Applications/RabbitViewer.app"
    if [[ -d "$APP_BUNDLE" ]]; then
        rm -rf "$APP_BUNDLE"
        green "  removed $APP_BUNDLE"
    fi
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

# 5. Write `rabbit` wrapper into ~/.local/bin.
#    The wrapper prepends the repo to PYTHONPATH so project modules win,
#    then delegates to the venv's `rabbit` entry point (installed by pip).
#    Re-run install.sh if the repo is moved.
mkdir -p "$BIN_DIR"

RABBIT_EP="$VENV_DIR/bin/rabbit"
RABBIT_DST="$BIN_DIR/rabbit"

if [[ ! -f "$RABBIT_EP" ]]; then
    red "Entry point not found after install: $RABBIT_EP"
    exit 1
fi

rm -f "$RABBIT_DST"
cat > "$RABBIT_DST" <<WRAPPER
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec "${RABBIT_EP}" "\$@"
WRAPPER
chmod +x "$RABBIT_DST"
green "  $RABBIT_DST → $RABBIT_EP"

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

# 5d. Install .desktop entry and icon (Linux only).
if [[ "$(uname)" == "Linux" ]]; then
    DESKTOP_SRC="$REPO_DIR/rabbitviewer.desktop"
    DESKTOP_DIR="$HOME/.local/share/applications"
    ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    ICON_SRC="$REPO_DIR/logo/rabbitViewerLogo.png"

    mkdir -p "$DESKTOP_DIR" "$ICON_DIR"

    # Copy icon.
    if [[ -f "$ICON_SRC" ]]; then
        cp "$ICON_SRC" "$ICON_DIR/rabbitviewer.png"
        green "  icon → $ICON_DIR/rabbitviewer.png"
    else
        yellow "  logo not found at $ICON_SRC — skipping icon install"
    fi

    # Install desktop entry.
    if [[ -f "$DESKTOP_SRC" ]]; then
        cp "$DESKTOP_SRC" "$DESKTOP_DIR/rabbitviewer.desktop"
        green "  desktop entry → $DESKTOP_DIR/rabbitviewer.desktop"
    else
        yellow "  desktop file not found at $DESKTOP_SRC — skipping"
    fi

    # Update desktop database if available.
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    fi
fi

# 5e. Build and install .app bundle (macOS only).
if [[ "$(uname)" == "Darwin" ]]; then
    APP_DIR="$HOME/Applications/RabbitViewer.app"
    CONTENTS="$APP_DIR/Contents"
    MACOS_DIR="$CONTENTS/MacOS"
    RESOURCES="$CONTENTS/Resources"
    ICON_SRC="$REPO_DIR/logo/rabbitViewerLogo.png"

    rm -rf "$APP_DIR"
    mkdir -p "$MACOS_DIR" "$RESOURCES"

    # -- PkgInfo --
    printf 'APPL????' > "$CONTENTS/PkgInfo"

    # -- Info.plist --
    cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>RabbitViewer</string>
    <key>CFBundleDisplayName</key>
    <string>RabbitViewer</string>
    <key>CFBundleIdentifier</key>
    <string>com.rabbitviewer.app</string>
    <key>CFBundleVersion</key>
    <string>0.1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleExecutable</key>
    <string>RabbitViewer</string>
    <key>CFBundleIconFile</key>
    <string>appicon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>Image</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>public.image</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
PLIST

    # -- Launcher script --
    cat > "$MACOS_DIR/RabbitViewer" <<LAUNCHER
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec "${RABBIT_EP}" viewer "\$@"
LAUNCHER
    chmod +x "$MACOS_DIR/RabbitViewer"

    # -- Icon: convert PNG → icns using macOS built-ins --
    if [[ -f "$ICON_SRC" ]]; then
        ICONSET_DIR="$(mktemp -d)/appicon.iconset"
        mkdir -p "$ICONSET_DIR"
        for size in 16 32 64 128 256 512; do
            sips -z "$size" "$size" "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}.png" &>/dev/null
            double=$((size * 2))
            sips -z "$double" "$double" "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" &>/dev/null
        done
        iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES/appicon.icns" 2>/dev/null \
            && green "  icon → $RESOURCES/appicon.icns" \
            || yellow "  iconutil failed — app will use default icon"
        rm -rf "$(dirname "$ICONSET_DIR")"
    else
        yellow "  logo not found at $ICON_SRC — app will use default icon"
    fi

    # Register with Launch Services so Spotlight indexes the app immediately.
    LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if [[ -x "$LSREGISTER" ]]; then
        "$LSREGISTER" -f "$APP_DIR" 2>/dev/null || true
    fi

    green "  app bundle → $APP_DIR"
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
green "Run: rabbit [directory]"
green "CLI: rabbit --help"
