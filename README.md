# RabbitViewer

A fast, daemon-backed image viewer for photographers and power users — built with Python and Qt6.

RabbitViewer separates rendering and metadata work from the interface. Heavy operations run in a persistent background process, while the GUI remains responsive even with very large libraries.

---

## Overview

RabbitViewer is designed around a strict separation of concerns:

* The **daemon** performs thumbnail generation, EXIF extraction, database writes, hashing, and file watching.
* The **GUI** focuses purely on interaction and presentation.

This architecture ensures predictable latency, smooth scrolling, and immediate UI feedback — even during large recursive scans or RAW-heavy workloads.

---

## Core Features

### Responsive by Design

Heavy work runs in a background daemon process. The GUI never blocks on decoding, hashing, or metadata extraction.

### Progressive Thumbnails

Images render as they are decoded.
Visible items are automatically prioritized by the scheduler — what you see loads first.

### Star Ratings

* Written back to file EXIF via ExifTool
* Persisted locally in SQLite
* Filterable in real time

### EXIF Metadata Display

Shutter speed, aperture, ISO, focal length, lens, camera body, and more.

### Live File Watching

Library updates automatically when files are added, moved, or deleted.

### Recursive Directory Scanning

Scan entire directory trees with optional recursive mode.

### Advanced Selection

* Range selection
* Select all
* Invert selection
* Undo / redo

### Full Image Viewer

* Zoom
* Pan
* Fast switching
* Pixel-level inspector overlay

### Fully Rebindable Hotkeys

Every action is configurable in `config.yaml`.
Multiple key sequences per action are supported.

### Python Script Automation

Drop plain Python files into `scripts/` and bind them to hotkeys.

### Plugin System

Add support for new formats by dropping a file into `plugins/`.

---

## Supported Formats

### Standard Image Formats (via Pillow)

* JPEG
* PNG
* BMP
* GIF
* TIFF
* WebP

### RAW Formats (via ExifTool preview extraction)

* Canon CR2, CR3
* Nikon NEF / NRW
* Sony ARW / SR2 / SRF
* Fujifilm RAF
* Olympus ORF
* Panasonic RW2
* Pentax PEF
* Leica RWL
* Hasselblad 3FR / FFF
* Mamiya MEF / MOS
* Phase One IIQ / CAP / EIP
* Samsung SRW
* Adobe DNG

New formats can be added by implementing:

```
get_thumbnail()
get_metadata()
set_rating()
```

Plugins are auto-discovered at startup.

---

## Hotkeys

All hotkeys are defined in `config.yaml`.

| Action               | Default         |
| -------------------- | --------------- |
| Next image           | D / →           |
| Previous image       | A / ←           |
| Return to thumbnails | Esc / Q         |
| Zoom in / out        | Ctrl++ / Ctrl+- |
| Toggle inspector     | I               |
| Filter               | Ctrl+F          |
| Range selection      | S (hold)        |
| Select all           | Ctrl+A          |
| Invert selection     | Shift+I         |
| Set rating 0–4       | 0 – 4           |
| Delete selected      | Del / R         |

You can bind any hotkey to a script:

```yaml
hotkeys:
  script:my_script:
    sequence: Ctrl+M
    description: Run my custom script
```

---

## Scripts

Scripts are plain Python files inside `scripts/`.

Each must expose:

```python
def run_script(api, selected_images):
```

Available API methods:

* `get_selected_images()`
* `get_all_images()`
* `get_hovered_image()`
* `set_selected_images()`
* `add_images()`
* `remove_images()`
* `set_rating_for_images(paths, rating)`

Bundled scripts include:

* set_rating_0–4
* select_all
* invert_selection
* delete_selected
* sort_by_name

The hotkey system acts as the automation entry point.

---

## Installation

### Prerequisites

* Python 3.10–3.13 (PySide6 does not yet support 3.14+)
* ExifTool (required for RAW support and writing ratings)

**macOS**

```
brew install exiftool
```

**Debian / Ubuntu**

```
sudo apt install libimage-exiftool-perl
```

---

### Install / Update

```
git clone https://github.com/yourname/RabbitViewer.git
cd RabbitViewer
./install.sh
```

The install script:

* Creates a virtualenv inside the repo using a compatible Python (3.10–3.13)
* Checks that ExifTool is installed and prints install instructions if not
* Installs the package in editable mode (source changes take effect immediately)
* Writes `rabbitviewer` and `rabbitviewer-daemon` wrappers into `~/.local/bin/`
* Installs the `rabbit` CLI dispatcher (see [CLI Tools](#cli-tools) below)
* Sets up shell completions for `rabbit` (bash and zsh)
* Adds `~/.local/bin` to your shell's PATH automatically if it isn't there already

**To update**, just re-run the script from the repo directory:

```
./install.sh
```

**To do a clean reinstall** (wipes and rebuilds the virtualenv):

```
./install.sh --clean
```

**To uninstall** the CLI wrappers:

```
./install.sh --uninstall
```

With Canon CR3 support, install the optional extras afterwards:

```
venv/bin/pip install ".[cr3]"
```

---

## Getting Started

Once installed, run from any directory:

```
rabbitviewer /path/to/photos
```

The daemon starts automatically if it is not already running. Thumbnails render progressively as the daemon processes files.

Options:

```
rabbitviewer /path/to/photos --no-recursive   # flat scan only
rabbitviewer /path/to/photos --restart-daemon  # force a fresh daemon
```

Logs are written to `~/.rabbitviewer/rabbitviewer.log`.

---

## CLI Tools

The `rabbit` command provides access to standalone CLI utilities:

```
rabbit --help              # list available commands
rabbit move-selected /dst  # move the current GUI selection to a directory
rabbit send-stop-signal    # gracefully shut down the daemon
```

Tab completion works in both bash and zsh after running `install.sh`.

New tools are added by dropping a `.py` file into `cli/` — they are discovered automatically as subcommands (underscores become hyphens, e.g. `my_tool.py` → `rabbit my-tool`).

---

## Architecture

RabbitViewer runs as two cooperating processes:

* `rabbitviewer_daemon.py`
* `main.py`

### IPC

* Unix domain socket
* `/tmp/rabbitviewer_thumbnailer.sock`
* Length-prefixed JSON protocol

### Scheduling

The `RenderManager` uses a priority queue.
Visible images always jump to the highest priority.

### Work Model

* SourceJob pattern discovers file paths
* Task factory converts paths into render tasks

### Database

SQLite (WAL mode) stores:

* Thumbnails
* EXIF metadata
* Ratings
* Content hashes

### File Watching

Based on watchdog.
Startup delay (30 seconds) avoids race conditions during large initial scans.

### Internal Communication

GUI uses an internal pub/sub EventSystem.

### Stack

* PySide6 (Qt6)
* SQLite
* watchdog
* Pillow
* ExifTool

---

## Running Tests

```
pytest tests/
```

---

## Philosophy

RabbitViewer is built for speed, determinism, and extensibility.

* No blocking UI.
* No opaque automation.
* No hidden state.

Just a clean, inspectable system that scales from a small shoot to a multi-terabyte archive.
