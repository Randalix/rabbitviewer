# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

RabbitViewer is a **daemon + GUI image viewer** built in Python. The GUI (PySide6/Qt6) is a thin client that communicates with a background daemon over a Unix domain socket. The daemon handles all heavy lifting: thumbnail generation, metadata extraction, database management, and file watching.

## Running the Application

```bash
# Terminal 1: Start the daemon
python rabbitviewer_daemon.py

# Terminal 2: Start the GUI (optionally with a directory)
python main.py [directory] [--recursive | --no-recursive]
```

## Running Tests

```bash
pytest tests/
# or a single file:
pytest tests/test_thumbnail_manager.py
```

## Architecture

### Client-Daemon Split

- **GUI** (`main.py` → `gui/`) — renders state, sends requests, reports viewport visibility
- **Daemon** (`rabbitviewer_daemon.py` → `core/`, `network/`) — processes images, manages DB, watches filesystem
- **IPC** — Unix socket at `/tmp/rabbitviewer_thumbnailer.sock`, JSON messages with 4-byte length-prefixed framing. Protocol is defined in `PROTOCOL.md` and `network/protocol.py` (Pydantic schemas).

### RenderManager (`core/rendermanager.py`)

The central orchestration engine. Key concepts:

- **SourceJob** — a self-contained workflow blueprint with a *generator* (discovers file paths) and a *task factory* (converts paths → `RenderTask`s). This decouples discovery from processing; domain logic lives in managers like `ThumbnailManager`, not in `RenderManager`.
- **RenderTask** — individual work unit. Task ID format: `{job_id}::{priority}::{image_path}::{task_type}`. Job ID conventions: `gui_scan::{session_id}::{directory}`, `watchdog::{path}`, `maintenance::{session_id}::{path}`.
- **Priority levels** (higher = more urgent): `BACKGROUND_SCAN` (10) → `LOW` (30) → `NORMAL` (50) → `HIGH` (70) → `GUI_REQUEST` (90) → `SHUTDOWN` (999). Visible thumbnails get graduated priorities via a Manhattan-distance heatmap (ring 0 = 90, ring 10 = 60, STEP=3). `IntEnum` supports unnamed members for intermediate values like `Priority(87)`.
- **Heatmap prioritization** (`core/heatmap.py`): The GUI computes a 10-ring diamond for thumbnails and a 4-ring diamond (offset by 3) for speculative fullres pre-caching. Priorities interleave: `T0(90) T1(87) T2(84) T3/F0(81) …`. Scan generator runs at 80; fullres drops below scan after ring 1. Delta-only IPC sends only changed paths. A generation counter drops stale viewport updates.
- **Two-phase scan**: Reconcile scan runs at Priority(80) with `create_tasks=False` for fast directory discovery. After scan completes, a `post_scan::` SourceJob creates tasks at `LOW` (30). The heatmap is the only mechanism that promotes tasks into the visible range.
- **Cooperative cancellation**: `RenderTask.cancel_event` (`threading.Event`) enables cancellation of speculative fullres tasks. `RenderManager.cancel_task()` sets the event and marks `is_active=False` for fast worker discard.
- **Backpressure**: Generators are throttled automatically if the queue grows too large.

### ThumbnailManager (`core/thumbnail_manager.py`)

Wraps `RenderManager` for image-specific work. Creates `SourceJob`s for thumbnail generation, metadata extraction, and rating write-back. Owns the plugin registry. Also handles speculative fullres pre-caching (`request_speculative_fullres`) and batch cancellation (`cancel_speculative_fullres_batch`).

### Shared Data Types (`core/priority.py`)

Qt-free enums and dataclasses used across daemon, plugins, and scripts: `Priority`, `TaskState`, `TaskType`, `SourceJob`, `RenderTask`. `RenderTask` supports cooperative cancellation via `cancel_event` and fast discard via `is_active`.

### Plugin System (`plugins/`)

Format handlers implement `get_thumbnail()`, `get_metadata()`, `set_rating()`. Built-ins: `cr3_plugin.py` (Canon RAW) and `pil_plugin.py` (JPEG, PNG, TIFF, etc.). New plugins are auto-discovered from the directory.

### MetadataDatabase (`core/metadata_database.py`)

SQLite with WAL mode for concurrent read/write. Stores EXIF, star ratings, thumbnail paths, content hashes. Thread-safe with `Lock`.

### EventSystem (`core/event_system.py`)

Pub-sub used to decouple GUI components from each other. Event types: `MOUSE_*`, `KEY_*`, `SELECTION_*`, `VIEW_*`, `ZOOM_*`, `STATUS_*`, `DAEMON_NOTIFICATION`. History limited to 500 events.

### GUI (`gui/`)

- `main_window.py` — top-level window, coordinates views
- `thumbnail_view.py` — grid/list thumbnail display (Qt Model-View)
- `picture_view.py` — full image viewer with zoom/pan
- `inspector_view.py` — pixel-level inspection overlay
- `hotkey_manager.py` — keyboard shortcut dispatch

### Other Notable Modules

- `core/heatmap.py` — pure-function module for Manhattan ring-distance priority computation (no Qt/daemon deps)
- `core/directory_scanner.py` — file discovery with ignore patterns and min-size filtering (batch_size=10 for fast priority responsiveness)
- `filewatcher/watcher.py` — watchdog-based filesystem monitoring; initial scan delayed 30s to avoid startup races; submits changes at `LOW` priority
- `core/selection.py` — selection state with undo/redo
- `scripts/` — user-extensible Python scripts with `ScriptAPI`; example scripts in `scripts/set_rating_*.py`
- `config/config_manager.py` — YAML config (`config.yaml` in project root, auto-generated with defaults)

## Key Conventions (from `CONVENTIONS.md` and `ARCHITECTURE.md`)

**`ARCHITECTURE.md` is the canonical reference for system design.** After any structural change — new module, new IPC message, changed task ID format, new priority level, plugin additions — update `ARCHITECTURE.md` to match.

- GUI is a thin client; keep heavy logic in the daemon.
- Use `EventSystem` for GUI-to-GUI communication, not direct method calls.
- Use the `SourceJob` pattern for any new background work introduced into `RenderManager`.
- Docstrings target an expert audience — keep them minimal and precise.
- Session-scoped job IDs are critical for priority isolation; never upgrade watchdog tasks to `GUI_REQUEST`.
