# RabbitViewer Architecture

## Overview

RabbitViewer is a daemon + GUI image viewer. The GUI is a thin Qt6 client; all heavy work runs in the daemon process.

```
┌─────────────────────────────┐     Unix socket      ┌──────────────────────────────────┐
│           GUI               │◄────────────────────►│           Daemon                 │
│   (main.py → gui/)          │  JSON + 4-byte frame  │  (rabbitviewer_daemon.py)        │
│                             │                       │                                  │
│  renders state              │   request/response    │  ThumbnailManager                │
│  sends requests             │   notifications ──►   │  RenderManager                   │
│  reports viewport           │                       │  MetadataDatabase                │
└─────────────────────────────┘                       │  DirectoryScanner                │
                                                       │  WatchdogHandler                 │
                                                       │  SocketServer                    │
                                                       └──────────────────────────────────┘
```

## Process Startup

```
main.py --restart-daemon [directory]
  1. Kill any existing daemon (if --restart-daemon)
  2. Spawn rabbitviewer_daemon.py via subprocess (start_new_session=True)
  3. Wait for socket file to appear (up to 10s)
  4. Start NotificationListener thread
  5. Open MainWindow
```

The daemon creates its socket file as the first act after startup, signaling readiness.

---

## Daemon

### Entry Point — `rabbitviewer_daemon.py`

Wires together:
- `ThumbnailSocketServer` on the Unix socket
- `ThumbnailManager` (owns `RenderManager`)
- `WatchdogHandler` (filesystem monitor)

Signal handlers (`SIGTERM`, `SIGINT`) trigger graceful shutdown.

---

### RenderManager — `core/rendermanager.py`

The central scheduling engine. Manages a priority queue of `RenderTask`s executed by a fixed worker thread pool.

**Priority levels** (higher = more urgent):

| Name | Value | Use |
|---|---|---|
| `BACKGROUND_SCAN` | 10 | DB consistency checks |
| `CONTENT_HASH` | 20 | Full file hashing |
| `LOW` | 30 | Watchdog-submitted tasks |
| `GUI_REQUEST_LOW` | 40 | Background GUI scan (slow job) / scrolled-away thumbnails |
| `NORMAL` | 50 | Standard scans |
| `HIGH` | 70 | Important on-demand tasks |
| `GUI_REQUEST` | 90 | Directly user-triggered work / heatmap ring 0 |
| `FULLRES_REQUEST` | 95 | Explicit fullres view image request |
| `SHUTDOWN` | 999 | Sentinel |

Visible thumbnails receive graduated priorities via a Manhattan-distance heatmap (see `core/heatmap.py`): ring 0 = 90, ring 1 = 87, …, ring 10 = 60, stepping by 3 per ring. `IntEnum` supports unnamed members for intermediate values like `Priority(87)`.

**Key data structures:**

- `task_graph: Dict[str, RenderTask]` — single source of truth; entries for PENDING/PAUSED/RUNNING tasks only (completed tasks are pruned once all dependents finish)
- `task_queue: PriorityQueue[RenderTask]` — runnable tasks ordered by `(priority DESC, timestamp ASC)`
- `active_jobs: Dict[str, SourceJob]` — currently running generator-based workflows

**Task lifecycle:**

```
submit_task()
  → PENDING (waiting for dependencies)
  → PAUSED (queued, awaiting worker)
  → RUNNING (executing in worker thread)
  → COMPLETED / FAILED → pruned from task_graph if no dependents remain
```

Priority upgrade invalidates the old `RenderTask` in the queue (sets `is_active=False`) and enqueues a new one at the higher priority. Workers discard stale invalidated tasks. The `cancel_event` (`threading.Event`) is preserved across upgrades and downgrades.

**Cooperative cancellation:**

`cancel_task(task_id)` sets the task's `cancel_event` and marks `is_active=False` for fast worker discard. `cancel_tasks(task_ids)` does the same in batch under a single lock acquisition. Workers check `cancel_event.is_set()` before executing the task function and skip silently if cancelled. Used for speculative fullres pre-caching that leaves the heatmap zone.

**SourceJob pattern:**

All multi-file workflows are `SourceJob`s — a generator (discovers paths) paired with a task factory (converts each path to `RenderTask`s). `_cooperative_generator_runner` processes one item per worker invocation and reschedules itself, enabling backpressure-friendly, interruptible scanning without blocking the worker pool. When queue depth exceeds `backpressure_threshold`, the next generator slice is throttled to `Priority.LOW`.

`SourceJob.task_priority` (optional) decouples the generator runner priority from child task priority. When set, child tasks are created at `task_priority` instead of `job.priority`, allowing the generator to run fast while tasks start low and await heatmap upgrades.

`scan_complete` is emitted **before** `on_complete` runs, so the GUI receives the scan-done signal before any `previews_ready` notifications from tasks created by `on_complete`. `scan_progress` is suppressed for `post_scan::` jobs to avoid duplicating already-known entries in the GUI model.

```
submit_source_job(job)
  → job_slice::job_id::0  →  job_slice::job_id::1  →  …  →  scan_complete notification
```

---

### ThumbnailManager — `core/thumbnail_manager.py`

Domain layer over `RenderManager`. Owns all image-specific workflows and the task operation registry for generic daemon task dispatch (used by `ScriptAPI.daemon_tasks()`). Registered operations: `send2trash`, `remove_records`.

**Tag write-back:** `_write_tags_to_file` mirrors the rating write-back pattern — suppresses watchdog, resolves plugin, calls `plugin.write_tags()` which clears then rewrites `XMP:Subject` via exiftool. Queued at `NORMAL` priority after the DB update completes synchronously.

**Two-phase scan flow for a GUI directory request:**

```
get_directory_files (socket command)
  │
  ├── Response: return DB-cached files + thumbnail_paths dict immediately
  │     (single batch SELECT, zero filesystem access on source files)
  │
  ├── Cached folder (DB has entries):
  │     GUI receives thumbnail_paths in the initial response.
  │     _tick_label_creation loads QImages inline as labels are born —
  │     no daemon round-trip, no heatmap request, no notification needed.
  │     Reconcile walk deferred 3s via threading.Timer at BACKGROUND_SCAN (10).
  │     Uncached files still go through the normal heatmap → notification flow.
  │
  └── New folder (empty DB):
        Phase 1: reconcile_job (Priority(80), create_tasks=False)
          Generator: scan_incremental_reconcile — discovers files,
          emits scan_progress so placeholders appear in the GUI.
          Accumulates all paths in ReconcileContext.discovered_files.
          No child tasks created — workers stay free for scanning.

        Phase 2: post_scan job (Priority.LOW, task_priority=LOW, on_complete)
          Submitted by on_complete after reconcile_job exhausts its generator.
          Iterates discovered_files → create_gui_tasks_for_file():
            ├── thumbnail already valid → no tasks (heatmap handles display)
            └── not valid → submit meta task + thumbnail task at LOW (30)
          Heatmap upgrades visible tasks to 60-90; non-visible tasks
          process in the background after all visible work completes.
```

For new folders, Phase 1 runs at priority 80 (above `HIGH`) so directory discovery is never blocked. Phase 2 tasks start at `LOW` (30), well below any heatmap ring minimum (60). The heatmap is the only mechanism that promotes tasks into the visible priority range.

For cached folders, the deferred reconcile walk detects new/deleted files after 3 seconds at `BACKGROUND_SCAN` priority. Stale thumbnails display briefly from cache until the walk re-validates and triggers regeneration. The GUI requests the full visible viewport at `GUI_REQUEST_LOW` (40) for immediate display.

**Heatmap priority flow** (viewport scroll / hover → graduated priorities):

```
GUI: _prioritize_visible_thumbnails()
  → compute_heatmap(center_row, center_col, columns, total_visible, loaded_set)
  → delta computation: only paths whose priority changed since last update
  → socket: update_viewport_heatmap(thumb_upgrades, downgrades, fullres_requests, fullres_cancels)
  → daemon: per-path request_thumbnail(path, Priority(priority))
            per-path request_speculative_fullres(path, Priority(priority))
            batch cancel_speculative_fullres_batch(paths)
```

The heatmap center is the hovered cell (if hovering) or viewport center (if not). A generation counter drops stale updates from the backed-up executor queue. Delta-only IPC: the GUI tracks `{path: priority}` dicts and sends only entries that changed. On directory change, `_clear_view` sends a batch cancel for all active speculative fullres paths so workers don't waste time on files no longer visible.

The first heatmap fires from `_update_filtered_layout` (via `_needs_heatmap_seed` flag) rather than from `scan_progress`, because `_visible_to_original_mapping` is not populated until after asynchronous label creation and layout update complete.

**Speculative fullres pre-caching:**

`request_speculative_fullres(path, priority, session_id)` submits a `view::{path}` task with a `cancel_event`. If the task already exists, it upgrades priority and preserves the existing `cancel_event`. `cancel_speculative_fullres_batch(paths)` cancels all out-of-zone tasks cooperatively.

Tasks that don't yet exist in the graph (e.g., already-valid files for which the slow scan hasn't run yet) are handled when the slow scan eventually reaches them.

**Task ID conventions:**

```
meta::{file_path}        — fast metadata scan (orientation, rating, file_size)
meta_full::{file_path}   — deferred full exiftool extraction (all EXIF fields, BACKGROUND_SCAN priority)
{file_path}              — thumbnail + view image generation task
view::{file_path}        — speculative fullres pre-cache (has cancel_event)
exif_rating::{file_path} — EXIF star rating write-back
write_tags::{file_path}  — XMP:Subject tag write-back
job_slice::{job_id}::{n} — nth cooperative slice of a SourceJob
script_task::{counter}   — compound task from script daemon_tasks API

Job ID prefixes:
gui_scan::{session}::{dir}   — Phase 1 reconcile scan (create_tasks=False)
post_scan::{session}::{dir}  — Phase 2 task creation from discovered files
watchdog::{path}             — filesystem watcher changes
maintenance::{session}::{path} — DB maintenance
```

---

### Plugin System — `plugins/`

Format handlers registered in a global `PluginRegistry` singleton (`plugins/base_plugin.py`).

**Lifecycle:** `load_plugins_from_directory` uses `importlib.util.spec_from_file_location` with the fully-qualified module name (`plugins.{module_name}`) so that `from .base_plugin import BasePlugin` relative imports resolve correctly. Plugins register themselves in `__init__` if `is_available()` returns True.

**Built-in plugins:**

| Plugin | Formats | Dependency |
|---|---|---|
| `PILPlugin` | `.jpg .jpeg .png .bmp .gif .tiff .tif .webp` | Pillow |
| `CR3Plugin` | `.cr3` | exiftool (CLI) |
| `RawPlugin` | `.nef .nrw .arw .sr2 .srf .dng .raf .orf .rw2 .pef .srw .mrw .rwl .3fr .fff .mef .mos .iiq .cap .eip .cr2` | exiftool (CLI) |
| `VideoPlugin` | `.mp4 .mov .mkv .avi .webm .m4v .wmv .flv .mpg .mpeg .3gp .ts` | ffmpeg + ffprobe (CLI) |

Each plugin implements: `process_thumbnail()`, `process_view_image()`, `generate_thumbnail()`, `generate_view_image()`, `extract_metadata()`, `write_rating()`, `write_tags()`, `is_available()`.

---

### MetadataDatabase — `core/metadata_database.py`

SQLite (WAL mode) storing per-file: EXIF metadata, star ratings, tags, thumbnail + view image cache paths, content hashes. Thread-safe via `threading.Lock` (single shared connection). A module-level singleton (`get_metadata_database`) prevents multiple connections to the same path.

**Tag system** uses a normalized schema: `tags` table (id, name, kind) + `image_tags` junction table (file_path, tag_id) with CASCADE deletes. Tags have a `kind` field (`'keyword'` or `'workflow'`). Keywords are auto-discovered from `XMP:Subject` / `IPTC:Keywords` during metadata extraction. CRUD methods: `get_or_create_tag`, `add_image_tags`, `remove_image_tags`, `set_image_tags`, `batch_set_tags`, `batch_remove_tags`, `get_image_tags`, `get_all_tags`, `get_directory_tags`. `get_filtered_file_paths` accepts an optional `tag_names` parameter that adds a junction-table subquery filter.

**Thumbnail validity** has two modes:

- `is_thumbnail_valid` / `batch_get_thumbnail_validity` — **strict**: calls `os.stat()` on the source file to compare mtime/size against the DB. Used by reconcile scan and task functions.
- `get_cached_thumbnail_paths` / `batch_get_cached_thumbnail_validity` — **trust-cache**: DB-only check, verifies only that the local thumbnail file exists. No `os.stat()` on the source. Used by the heatmap fast path (`request_thumbnail`, `batch_request_thumbnails`) so cached folders display without NAS latency.

`_store_metadata` (called by background metadata extraction) updates all fields including `rating`, so externally-set ratings are picked up on re-scan. `set_rating` updates only the DB rating synchronously; the EXIF write-back is queued separately at `LOW` priority.

---

### Shared Data Types — `core/priority.py`

Qt-free enums and dataclasses shared across daemon, plugins, and scripts: `Priority`, `TaskState`, `TaskType`, `SourceJob`, `RenderTask`. `SourceJob` has an optional `task_priority` field for decoupling generator priority from child task priority. `RenderTask` supports cooperative cancellation via `cancel_event` (`threading.Event`) and fast worker discard via `is_active`.

### Heatmap — `core/heatmap.py`

Pure-function module (no Qt or daemon dependencies) for Manhattan ring-distance priority computation. A 10-ring diamond around the cursor assigns thumbnail priorities from 90 (ring 0) to 60 (ring 10), stepping by 3 per ring. A 4-ring diamond offset by 3 assigns speculative fullres priorities (ring 0 = 81, ring 4 = 69), interleaving with thumbnails so nearby thumbs render before fullres begins. The scan generator runs at priority 80; fullres ring 0 (81) beats the scan, but ring 1 (78) yields to it.

`compute_heatmap(center_row, center_col, columns, total_visible, loaded_set)` returns `(thumb_pairs, fullres_pairs)` as `[(visible_idx, priority), ...]`. Uses bounding-box clipping to avoid scanning the entire grid.

### DirectoryScanner — `core/directory_scanner.py`

Yields file paths from a directory walk. Applies ignore patterns (glob) and min-size filtering. Supported extensions are cached at construction time from the plugin registry. `scan_incremental` is the generator used by `scan_directory` and directly by `socket_thumbnailer.py`; it yields batches of 10 paths for cooperative use in SourceJobs (small batches improve priority responsiveness). `scan_incremental_reconcile` wraps `scan_incremental` with a `ReconcileContext`: discards found files from `db_file_set`, accumulates all paths in `discovered_files` for post-scan task creation, and leaves `ghost_files` (DB entries missing from disk) after exhaustion. Uses batches of 50.

---

### WatchdogHandler — `filewatcher/watcher.py`

Watchdog-based filesystem monitor. Initial scan is delayed 30 s after startup to avoid races with the GUI scan. Submits changed/added files at `LOW` priority. Never upgraded to `GUI_REQUEST` — session-isolated.

---

## IPC

### Socket Protocol — `network/protocol.py`

Unix socket at `/tmp/rabbitviewer_thumbnailer.sock` (configurable). Messages are length-prefixed JSON: 4-byte big-endian `uint32` followed by the UTF-8 JSON body. Schemas are Pydantic models.

Two channels:
- **Request/response** — `ThumbnailSocketClient` ↔ `ThumbnailSocketServer`
- **Notifications** — `NotificationListener` maintains a persistent connection; daemon pushes `previews_ready`, `scan_progress`, `scan_complete`, `files_removed` events

**Key request types:**
- `update_viewport` — carries per-path `PathPriority` pairs for thumb upgrades, downgrade paths, fullres requests, and fullres cancels. See `PROTOCOL.md` for full schema.
- `set_tags` / `remove_tags` — bulk tag assignment/removal for image paths. DB update is synchronous; XMP write-back is queued asynchronously.
- `get_tags` — returns two-tier tag lists (directory-scoped + global) for autocomplete.
- `get_image_tags` — returns per-path tag lists for a set of images.
- `get_filtered_file_paths` — extended with optional `tag_names` for combined text + star + tag filtering.

See `PROTOCOL.md` for full message schema reference.

### GUI Control Socket — `network/gui_server.py` / `network/gui_client.py`

A second socket (`/tmp/rabbitviewer_gui.sock`) lets CLI tools (e.g. `cli/move_selected.py`) send commands to a running GUI instance. Carries `execute_selection_command` messages.

---

## GUI

### MainWindow — `gui/main_window.py`

Top-level coordinator. Stacks `ThumbnailViewWidget`, `PictureView`, and `VideoView` in a `QStackedWidget`. Routes double-clicks and navigation to the correct view based on file extension (`_is_video()` helper). Owns `HotkeyManager`, `SelectionHistory`, and the script runner. Supports drag-and-drop of files/folders to load directories.

### ThumbnailViewWidget — `gui/thumbnail_view.py`

Grid display of file placeholders (`ThumbnailLabel`). Responsibilities:
- Receives `scan_progress` → creates placeholders
- Receives `previews_ready` → loads `QImage` from disk, updates label
- **Inline thumbnail loading**: `_tick_label_creation` checks `_initial_thumb_paths` (populated from the initial `get_directory_files` response) and loads cached QImages as labels are created — labels are born with thumbnails already set, no daemon round-trip needed
- Scroll / hover events → `_prioritize_visible_thumbnails` → heatmap computation → `update_viewport_heatmap` to daemon (delta-only IPC with generation counter for stale-request dropping)
- Tracks `_last_thumb_pairs` and `_last_fullres_pairs` (`dict[str, int]`) for delta detection
- Partitions `_pending_previews` so heatmap-zone items load first (O(N) partition, not sort)
- Filter + sort via daemon query

### PictureView — `gui/picture_view.py`

Full-resolution image viewer with zoom/pan. Created lazily on first image open (`WA_DeleteOnClose`). Consumes events from `EventSystem`.

### VideoView — `gui/video_view.py`

Embedded mpv player for video files. Uses `python-mpv` with `wid=str(int(self.winId()))` to render directly into the Qt widget. Same lifecycle as PictureView (`WA_DeleteOnClose`, lazy creation). Publishes `INSPECTOR_UPDATE` events with `norm_x = cursor_x / widget_width` for inspector timeline scrubbing. Keyboard controls: Space (pause), M (mute), `[`/`]` (seek ±5s).

### InspectorView — `gui/inspector_view.py`

Pixel-level inspection window. Works for both images and videos. For images: fetches view image from daemon, renders via `PictureBase`. For videos: uses a headless mpv instance (`vo="null"`, `ao="null"`) to decode frames on demand via `screenshot_raw()` → PIL → QImage → `PictureBase`. Mode mapping: TRACKING = spatial crop (image) / timeline scrub (video), FIT = fitted display, MANUAL = user-controlled pan (image) / user-controlled scrub (video).

### Modal Menu — `gui/modal_menu.py`, `gui/menu_registry.py`

Floating overlay menu system triggered by hotkeys. `MenuNode` defines a tree: each node has a `key` (single-char trigger), and either `children` (submenu), `script` (runs via ScriptManager), or `action` (arbitrary callable). The menu installs an `eventFilter` on `QApplication` (LIFO order, runs before `QShortcut`) to intercept all keys while open. `MenuContext` provides visibility predicates based on current view and selection state.

Menus are registered in `menu_registry.py` via `build_menus()` and bound to hotkeys with the `menu:` prefix in config (e.g., `menu:tags` → T key).

**Built-in menus:**
- `sort` — sort thumbnails by date/name/rating/size/type (thumbnail view only)
- `tags` — tag operations: Add/Edit (A, opens `TagEditorDialog`) and Filter (F, opens `TagFilterDialog`)

### Tag Dialogs — `gui/tag_editor_dialog.py`, `gui/tag_filter_dialog.py`

Two non-modal dialogs for tag operations, both using the reusable `TagInput` widget (`gui/components/tag_input.py`):

- **TagEditorDialog** — assignment popup for selected images. Pre-populates with the intersection of existing tags across the selection. On confirm, computes a diff (added/removed) and sends `set_tags` / `remove_tags` IPC commands.
- **TagFilterDialog** — standalone tag filter. Emits `tags_changed` signal wired to `ThumbnailViewWidget.apply_tag_filter()`.

`TagInput` is a `QLineEdit` subclass accepting comma-separated tags with two-tier `QCompleter` autocomplete (directory tags first, separator, global tags). Supports shell-style Tab completion (accepts first match) and Enter to confirm.

### Overlay System — `gui/overlay_manager.py`, `gui/overlay_renderers.py`

Script-triggered overlays on thumbnails. Flow: `ScriptAPI.show_overlay()` → `EventSystem` (`THUMBNAIL_OVERLAY`) → `ThumbnailViewWidget._on_overlay_event()` → `OverlayManager` → `ThumbnailLabel.paintEvent()`.

`OverlayManager` stores `OverlayDescriptor`s keyed by `(original_idx, overlay_id)`. Transient overlays auto-remove via `QTimer.singleShot(duration)`. Permanent overlays (`duration=None`) persist until explicitly removed. Multiple overlays stack freely per thumbnail via distinct `overlay_id`s. Bulk degradation gate: >50 transient overlays fall back to a status bar message.

Renderers are pluggable callables `(QPainter, QRect, dict) -> None` registered by name. Built-in: `stars` (gold star glyphs with dark pill), `badge` (colored circle with optional text). `_compute_sub_rect` maps position names (`center`, `top-left`, etc.) to sub-rects for corner placement.

### EventSystem — `core/event_system.py`

Pub-sub bus for GUI-internal communication. Typed event data classes; history capped at 100 via `deque`. Thread-safe: `subscribe`/`unsubscribe`/`publish` all hold a lock; `publish` snapshots the subscriber list before iterating so callbacks may safely call `subscribe`/`unsubscribe`. Use for all GUI→GUI state changes; never call GUI methods directly. Ephemeral event types (`INSPECTOR_UPDATE`, `THUMBNAIL_OVERLAY`) skip history to avoid evicting useful events.

---

## Configuration

`config/config_manager.py` loads `config.yaml` from the working directory, merging with `DEFAULT_CONFIG`. Dot-notation key access (`config.get("files.cache.dir")`). Written back on `set()`.

Key config paths:

```yaml
system.socket_path: /tmp/rabbitviewer_thumbnailer.sock
cache_dir: ~/.rabbitviewer
database_path: metadata.db
thumbnail_size: 128
watch_paths: [~/Pictures, ~/Downloads]
```

---

## Scripts

`scripts/` — user Python scripts executed via `ScriptAPI`. Each script exports `run_script(api)`. Loaded at startup; bound to hotkeys via `config.yaml`. Built-in examples: `set_rating_*.py`, `delete_selected.py`, `select_all.py`.

`ScriptAPI` provides overlay support: `show_overlay(paths, renderer, params, position, duration, overlay_id)` and `remove_overlay(paths, overlay_id)`. Rating scripts (0–5) show a transient star overlay for 1.2 s after rating.

---

## Benchmarks

Two top-level scripts measure end-to-end performance:

- **`bench_first_image.py`** — active benchmark: kills the daemon, purges thumbnail cache, starts a fresh daemon, and measures time-till-Nth-thumbnail via the socket protocol (no GUI). Run with `python3 bench_first_image.py ~/Pictures`.
- **`bench_sessions.py`** — passive log scraper: parses `image_viewer.log` and `daemon.log` to extract per-session metrics from real GUI usage — startup timeline, time to first image displayed, throughput, dropped notifications. Run with `python3 bench_sessions.py [--last N] [--dir FILTER]`.
