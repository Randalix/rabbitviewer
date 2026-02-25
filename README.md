# RabbitViewer

A fast, daemon-backed image viewer for photographers and power users — built with Python and Qt6.

RabbitViewer doesn’t just display images.
It orchestrates them.

Rendering, metadata extraction, hashing, and file watching run in a persistent background daemon. The interface stays fluid — even when you point it at a massive RAW archive.

[![RabbitViewer Demo](https://img.youtube.com/vi/XzhZ5Wn1O8U/maxresdefault.jpg)](https://youtu.be/XzhZ5Wn1O8U)

---

## Overview

RabbitViewer is built on a strict separation of concerns:

* The **daemon** handles thumbnail generation, EXIF extraction, database writes, hashing, and file watching.
* The **GUI** is dedicated purely to interaction and presentation.

The result is predictable latency, smooth scrolling, and immediate feedback — even during large recursive scans or RAW-heavy workloads.

You can scroll aggressively through thousands of files and the interface never stalls. The heavy lifting happens elsewhere.

---

## Core Features

### Responsive by Architecture

Decoding, hashing, and metadata extraction never block the UI thread.
The viewer remains interactive under load — always.

### Progressive Thumbnails

Images appear as soon as they’re decoded.

A heatmap radiates from the mouse cursor. Thumbnails closest to your pointer load first, decreasing outward across a 10-ring Manhattan diamond. Nearby images receive speculative full-resolution pre-caching within a 4-ring zone, with cooperative cancellation when you move.

The viewer anticipates you.

### Star Ratings

Ratings are:

* Written back to file EXIF via ExifTool
* Stored locally in SQLite
* Filterable instantly

No proprietary lock-in. Your metadata stays with your files.

### EXIF Metadata Display

Shutter speed, aperture, ISO, focal length, lens, camera body — immediately visible without leaving the viewer.

### Live File Watching

Add, move, or delete files — the library updates automatically.

No manual refresh. No rescans required.

### Recursive Directory Scanning

Scan entire directory trees, or stay flat. Your workflow, your choice.

### Advanced Selection

* Range selection
* Select all
* Invert selection
* Undo / redo

Designed for high-volume culling sessions.

### Video Playback

Integrated mpv playback supports modern video formats.
Scrub the timeline directly from the inspector using mouse position.

Switch seamlessly between stills and motion.

### Full Image Viewer

* Smooth zoom
* Fluid panning
* Fast image switching
* Pixel-level inspector overlay (images and videos)

Zero friction between browsing and inspection.

### Python Script Automation

Drop plain Python files into `scripts/` and bind them to actions.

RabbitViewer exposes a clean API surface for automation. You can batch-edit ratings, reorganize selections, or implement custom workflows in minutes.

### Plugin System

Extend format support by adding a file to `plugins/`.

Implement three functions:

```
get_thumbnail()
get_metadata()
set_rating()
```

Plugins are auto-discovered at startup.

RabbitViewer is designed to be extended — not forked.

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

### Video Formats (via ffmpeg + mpv)

* MP4, MOV, MKV, AVI, WebM, M4V
* WMV, FLV, MPG, MPEG, 3GP, TS

New formats can be added through plugins.

---

## Scripts

Scripts are plain Python files placed inside `scripts/`.

Each script must expose:

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

Automation is a first-class feature — not an afterthought.

---

## Installation

### Prerequisites

* Python 3.10–3.13 (PySide6 does not yet support 3.14+)
* ExifTool (required for RAW support and writing ratings)
* ffmpeg / ffprobe (required for video thumbnails and metadata)
* mpv + libmpv (required for video playback)

**macOS**

```
brew install exiftool ffmpeg mpv
```

**Debian / Ubuntu**

```
sudo apt install libimage-exiftool-perl ffmpeg libmpv-dev
```

---

### Install / Update

```
git clone https://github.com/Randalix/rabbitviewer.git
cd RabbitViewer
./install.sh
```

The install script:

* Creates a virtualenv using a compatible Python (3.10–3.13)
* Verifies dependencies
* Installs in editable mode
* Writes a `rabbit` CLI wrapper into `~/.local/bin/`
* Sets up shell completion
* Installs a launcher entry

To update:

```
./install.sh
```

Clean reinstall:

```
./install.sh --clean
```

Optional extras:

```
venv/bin/pip install ".[cr3]"
venv/bin/pip install ".[video]"
```

---

## Getting Started

From any directory:

```
rabbit /path/to/photos
```

The daemon starts automatically if needed. Thumbnails appear progressively as files are processed.

Options:

```
rabbit /path/to/photos --no-recursive
rabbit /path/to/photos --restart-daemon
```

Logs:

```
~/.rabbitviewer/rabbitviewer.log
```

---

## CLI Tools

The `rabbit` command also exposes standalone utilities:

```
rabbit --help
rabbit move-selected /dst
rabbit send-stop-signal
```

New subcommands are added by dropping `.py` files into `cli/`.
They are auto-discovered at startup.

---

## Architecture

RabbitViewer runs as two cooperating processes:

* `rabbitviewer_daemon.py`
* `main.py`

### IPC

* Unix domain socket
* Length-prefixed JSON protocol

### Scheduling

The `RenderManager` uses a heatmap-driven priority queue:

* 10-ring Manhattan diamond around the cursor
* Priorities from 90 (under cursor) to 40 (outer ring)
* 4-ring speculative full-resolution pre-cache zone
* Cooperative cancellation
* Delta-only IPC updates
* Generation counters drop stale updates during fast scroll

### Work Model

* SourceJob discovers file paths
* Task factory converts paths into render tasks
* RenderTask supports cooperative cancellation via `threading.Event`

### Database

SQLite (WAL mode) stores:

* Thumbnails
* EXIF metadata
* Ratings
* Content hashes

### File Watching

Based on watchdog.
Startup delay avoids race conditions during large initial scans.

### Stack

* PySide6 (Qt6)
* SQLite
* watchdog
* Pillow
* ExifTool
* ffmpeg / mpv

---

## Running Tests

```
pytest tests/
```

---

## Philosophy

RabbitViewer is built for speed, determinism, and extensibility.

No blocking UI.
No opaque automation.
No hidden state.

Just a fast, inspectable system that scales from a small shoot to a multi-terabyte archive.

