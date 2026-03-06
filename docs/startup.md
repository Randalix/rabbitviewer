# RabbitViewer — Startup, Bottlenecks, and Benchmarking

---

## 1. The Startup Chain

### 1.1 Daemon side (headless background indexer)

The daemon (`main.py --daemon`) runs independently from the GUI. It indexes
`watch_paths` from config and pauses when a GUI is active.

```
main.py --daemon → _run_daemon()
  │
  ├─ 1. Load config (config.yaml) + logging setup
  │
  ├─ 2. Acquire daemon PID lock (~/.rabbitviewer/cache/daemon.pid)
  │     flock(LOCK_EX|LOCK_NB) — exits if another daemon holds it
  │
  ├─ 3. _init_core():
  │     ├─ MetadataDatabase init (SQLite WAL at ~/.rabbitviewer/cache/metadata.db)
  │     ├─ ThumbnailManager + RenderManager (8 worker threads, priority queue)
  │     └─ load_plugins()
  │          • CR3Plugin.is_available() → subprocess("exiftool -ver")   ~100–400 ms
  │          • PILPlugin.is_available() → import PIL                    ~1 ms
  │
  ├─ 4. WatchdogHandler.start()
  │     Initial filesystem scan deliberately delayed 30 s
  │
  ├─ 5. BackgroundIndexer.start_indexing()
  │     Submits one SourceJob per watch_path at BACKGROUND_SCAN(10) priority
  │
  └─ 6. GUI flock poll loop (every 2 s)
        ├─ GUI active  → rm.pause()   (workers idle)
        └─ GUI gone    → rm.resume()  (workers resume indexing)
```

---

### 1.2 GUI side (in-process, no socket IPC)

The GUI runs all core services in-process. `ThumbnailService` is the facade
that replaces the old socket client. `DaemonSignals` bridges worker-thread
notifications to the Qt main thread via direct callback.

```
main.py [directory] → _run_gui()
  │
  ├─ 1. Load config
  │
  ├─ 2. Acquire GUI flock (~/.rabbitviewer/gui.lock)
  │     Tells daemon to pause workers
  │
  ├─ 3. _init_core():
  │     ├─ MetadataDatabase init
  │     ├─ ThumbnailManager + RenderManager
  │     └─ load_plugins()
  │
  ├─ 4. ThumbnailService(thumbnail_manager, directory_scanner)
  │     In-process facade — same API as old socket client
  │
  ├─ 5. WatchdogHandler.start()
  │
  ├─ 6. _auto_launch_daemon()
  │     Spawns detached daemon subprocess for post-GUI background indexing
  │
  ├─ 7. QApplication created
  │
  ├─ 8. DaemonSignals (QObject)
  │     Connected to RenderManager.add_notification_callback()
  │     Bridges worker-thread notifications → Qt signals on main thread
  │
  ├─ 9. MainWindow.__init__ (minimal — first paint focus)
  │     • _setup_thumbnail_view() → ThumbnailViewWidget created
  │     • Window title + resize(800, 600)
  │     • QTimer.singleShot(0, _deferred_init)
  │
  ├─ 10. window.show()
  ├─ 11. app.processEvents()   ← window paints first frame here
  │                              _deferred_init fires during this call
  │
  ├─ 12. QTimer.singleShot(0, load_directory)
  │
  └─ 13. app.exec()   ← event loop running, load_directory fires on first tick
```

**`_deferred_init` (fires during processEvents, before app.exec):**
- SelectionState + SelectionProcessor + SelectionHistory
- GuiServer (CLI interop socket at /tmp/rabbitviewer_gui.sock)
- ScriptManager (loads `scripts/` directory)
- Hotkey setup
- Event subscriptions

**`load_directory` → in-process request chain:**

```
ThumbnailViewWidget.load_directory(path, recursive)
  │
  ├─ _load_directory_deferred() in background thread
  │   │
  │   └─ service.get_directory_files(path)
  │         ── in-process call ──►  ThumbnailService
  │                                   │
  │                                   ├─ DB lookup: return cached files + thumbnail_paths immediately
  │                                   │
  │                                   ├─ Submit reconcile_job (Priority(80), create_tasks=False)
  │                                   │   scan_incremental_reconcile discovers new/deleted files
  │                                   │   Emits scan_progress so placeholders appear in the GUI
  │                                   │
  │                                   └─ on_complete → post_scan job (Priority.LOW)
  │                                       create_gui_tasks_for_file per discovered path:
  │                                         cached → no tasks (heatmap handles display)
  │                                         uncached → submit meta + thumbnail tasks at LOW(30)
  │
  └─ GUI receives get_directory_files response (synchronous return)
      • Cached files: _add_image_batch() immediately with thumbnail_paths (pre-populates grid)
      • New files arrive via scan_progress notifications
```

**GUI notification handling after load_directory:**

```
DaemonSignals (worker thread → Qt main thread)
  │
  ├─ scan_progress   → _add_image_batch() → placeholder labels
  │                    _filter_update_timer.start(200ms)
  │                        └─ _update_filtered_layout()
  │                              └─ _priority_update_timer.start(100ms)
  │                                    └─ _prioritize_visible_thumbnails()
  │
  ├─ previews_ready  → QImage(thumbnail_path)
  │                    emit _thumbnail_generated_signal
  │                        └─ _on_thumbnail_ready()
  │                              └─ label.updateThumbnail(pixmap)
  │
  └─ scan_complete   → reapply_filters() → final layout
```

**Viewport prioritisation (`_prioritize_visible_thumbnails`):**
Fires 100 ms after each layout update, and again on scroll/resize (debounced ~150 ms).

```
Visible paths → service.update_viewport_heatmap(upgrades, downgrades, fullres, cancels)
                    in-process: request_thumbnail(path, Priority(priority))
                              ├─ FAST PATH: is_thumbnail_valid? → put previews_ready directly (< 1 ms)
                              └─ SLOW PATH: update_task_priorities → worker upgrades to GUI_REQUEST
```

The fast path dominates for warm-cache libraries — it bypasses the task graph entirely and costs one SQLite read + one `os.path.exists` call.

---

### 1.3 End-to-end for a 2,225-file library

**Warm cache (all thumbnails on disk):**

| Event | Time from `load_directory` |
|---|---|
| First `scan_progress` | ~75 ms |
| First `previews_ready` | **< 200 ms** |
| `scan_complete` (full scan) | ~900 ms |

**Cold cache (first run, CR3 files on NAS):**

| Event | Time from `load_directory` |
|---|---|
| First `scan_progress` | ~75 ms |
| First `previews_ready` | ~1,700 ms |
| `scan_complete` | ~900 ms |

On cold cache, `scan_complete` arrives before the first `previews_ready` because the fast scan finishes before any thumbnail has been generated.

---

## 2. Bottlenecks

### 2.1 Plugin `is_available()` on startup

`CR3Plugin.is_available()` runs `subprocess.run(["exiftool", "-ver"])` — a cold subprocess spawn that costs 100–400 ms. This runs during `_init_core()` in both GUI and daemon modes. If exiftool is on a slow PATH or NFS-mounted, this can add 1–2 seconds before the window appears.

### 2.2 Cold-cache thumbnail generation

CR3 file processing on NAS is the dominant bottleneck on first run:

| Step | Cost per file |
|---|---|
| File header read (512 KB, NAS) | 0.5–2.0 s |
| Embedded thumbnail extraction (buffer) | ~0 ms (pure Python, buffer already read) |
| Exiftool fallback (`-ThumbnailImage`, stay-open) | 0.3–1.0 s |
| PIL decode + resize + save | 5–15 ms |
| **Total (buffer hit)** | **0.5–2.0 s** |
| **Total (exiftool fallback)** | **0.8–3.0 s** |

With 8 workers, throughput approaches `8 / mean_cost`. Buffer hit rate determines whether the exiftool process is needed at all — when the thumbnail is fully contained in the first 512 KB, no extra I/O is needed.

### 2.3 Sequential fast scan order

The fast scan emits files in filesystem order, not viewport order. Placeholder labels appear left-to-right, top-to-bottom in FS order. On warm cache, `previews_ready` also arrives in FS order, so files at the *bottom* of the visible grid may appear before files at the *top* of the next page. Viewport prioritisation corrects this for unloaded visible tiles, but there is an inherent tension between scan order and visual order.

### 2.4 Notification delivery from worker threads

`RenderManager` delivers notifications via registered callbacks (including `DaemonSignals.dispatch_notification`). Callbacks are invoked directly from worker threads; Qt's `AutoConnection` marshals the signal emission to the main thread. If the main thread is under heavy load (e.g., creating many placeholder labels), notification processing can back up.

### 2.5 `_deferred_init` and `load_directory` timing

Both `_deferred_init` and `load_directory` are scheduled via `QTimer.singleShot(0, ...)`. They fire in order (init first, then load), but both fire on the *same* event loop tick — the first `app.exec()` iteration. If `_deferred_init` is slow (e.g., ScriptManager loads many scripts), it delays `load_directory`. The window is visible but unresponsive until `_deferred_init` completes. Since `_init_core()` now runs before `QApplication` is created, plugin availability checks add to the pre-window delay.

### 2.6 NAS read amplification

`_read_file_header` reads 512 KB per file for the prefetch buffer (orientation + thumbnail extraction). The MD5 hash uses only the first 256 KB. If a CR3's thumbnail is positioned past the 512 KB mark (rare, but possible with large preview blocks), the buffer miss triggers an additional exiftool call — a second full NAS round-trip.

### 2.7 View-image generation load

Stage C (`gui_view_images` SourceJob) runs at `BACKGROUND_SCAN` priority (10), well below thumbnail work (40–90). Each CR3 view image takes 7–17 s on NAS via `exiftool -JpgFromRaw`. With a library of 2,000+ files, the view-image queue can run for hours. This is by design (thumbnails come first), but it adds sustained NAS load that competes with future thumbnail scans.

---

## 3. Potential Improvements

### 3.1 Inode + mtime cache key instead of MD5

Currently, `_read_file_header` reads 256 KB just to compute a hash for cache lookup. On NAS, this is the dominant I/O cost per file. Switching to `(inode, mtime, size)` as the cache key would reduce this to a single `stat()` call (~1 ms local, ~20 ms NAS) for warm-cache files.

Trade-off: inode recycling can cause false cache hits across renames/replacements on some filesystems. A hybrid — stat first, fall back to MD5 on mismatch — would cover the common case while remaining correct.

### 3.2 Viewport-sorted cold-cache task submission

The slow scan submits tasks in filesystem order. If the scan could be seeded with visible paths first (from the DB-cached file list returned by `get_directory_files`), the first viewport would be fully thumbnailed before the rest of the library. This is already partially addressed by viewport prioritisation, but a dedicated "first paint" job at `GUI_REQUEST` priority for the first screen's worth of files would eliminate the priority upgrade round-trip.

### 3.3 Async plugin availability checks

Move `is_available()` checks to a background task after the accept loop starts. The daemon can emit a `plugins_ready` notification once all plugins have been verified, and the GUI can start a scan immediately, knowing it may receive "unsupported format" for some files until the check completes. For the common case (exiftool installed), this removes the 100–400 ms startup penalty entirely.

### 3.4 Larger prefetch buffer for deep-embed thumbnails

CR3 files with unusually large metadata blocks occasionally have the embedded thumbnail beyond the current 512 KB window, forcing an exiftool fallback. Increasing the prefetch to 1 MB would improve buffer hit rates at the cost of 2× NAS I/O per file on the first pass.

### 3.5 Scan batch size tuning

The directory scanner yields batches of 50 files. Each batch causes one `scan_progress` notification and one layout update (after 200 ms debounce). For a 2,225-file library this is ~44 batches. Larger batches (e.g., 200) would reduce layout churn; smaller batches would update the grid more incrementally. The current value is a reasonable default but has not been tuned empirically.

### 3.6 Notification coalescing

Worker threads invoke notification callbacks directly for every cache-hit file. With large warm-cache libraries, this can flood the Qt signal queue. Coalescing multiple `previews_ready` notifications into batched signals would reduce main-thread wakeups.

### 3.7 Parallel exiftool processes

The current design uses one `ExifToolProcess` (stay-open exiftool) per worker thread. With 8 workers, this means up to 8 concurrent exiftool processes — each consuming ~30 MB RAM. An alternative is a shared exiftool process with request serialisation, which would reduce process count but serialize extraction. The current approach is better for throughput; the main constraint is NAS I/O parallelism, not process overhead.

---

## 4. Benchmarking

### 4.1 `bench_first_image.py` — active cold-cache benchmark

Measures wall-clock time from daemon `Popen()` (t=0) to each `previews_ready` notification, bypassing the Qt GUI entirely.

```bash
python3 bench_first_image.py ~/Pictures [--timeout 120] [--no-recursive]
```

**What it does:**
1. Kills any running daemon
2. Purges DB entries and thumbnail/view files for the target directory
3. Spawns a fresh daemon; records t=0
4. Registers as a notification listener (raw socket, no Qt)
5. Sends `get_directory_files` to start the three SourceJobs
6. Collects every `previews_ready` with a non-null `thumbnail_path`
7. Reports timeline milestones and throughput statistics

**Output format:**
```
Timeline (wall clock from t=0 = daemon Popen):
  t_socket   :    143.2 ms  daemon socket appeared
  t_notifier :    287.3 ms  notification listener registered
  t_scan_sent:    310.5 ms  get_directory_files sent
  t_scan_ack :    412.1 ms  SourceJobs submitted

Time-till-Nth-thumbnail (from t=0):
  t_1   :   1853.2 ms  ← first image
  t_5   :   2341.5 ms
  t_10  :   2891.2 ms
  t_25  :   3812.4 ms
  t_50  :   5213.7 ms

Processing latency (from SourceJobs submitted to notification):
  t_1   :   1441.1 ms  ← first image
  ...

Inter-notification gap (first 100):
  mean   :   92.1 ms
  median :   78.3 ms
  stdev  :  41.5 ms
  max    :  312.0 ms
```

The "processing latency" section subtracts `t_scan_ack` to isolate pure thumbnail generation cost from startup noise.

**When to run:** After any change to the thumbnail pipeline, plugin code, RenderManager, or startup sequence. Run twice (first run purges cache, second run measures warm cache) to get both baselines.

---

### 4.2 `bench_sessions.py` — passive log scraper

Extracts per-session metrics from `image_viewer.log` and `daemon.log` without running anything.

```bash
python3 bench_sessions.py [--last N] [--all] [--dir FILTER]
```

**What it extracts:**
- GUI startup time (process start → daemon connected → scan sent → scan ACK)
- Time-to-Nth-thumbnail milestones (1, 5, 10, 25, 50, 100)
- Warm vs. cold cache classification (first thumbnail < 400 ms from scan ACK → warm)
- Thumbnail throughput (thumbnails/s between first and last)
- Inter-notification gaps (mean, median, stdev, max, for first 100)
- Dropped notifications (daemon.log `"Notification queue full"` events)
- Session duration and exit code

**Example output:**
```
Session 12/12   started 2026-02-20 10:28:42
================================================================
  Directory : /Users/joe/Pictures  [recursive]

  Startup timeline (from GUI launch):
    t=0        GUI process start         10:28:42.274
    + 1203 ms  Daemon socket connected   10:28:43.477
    + 1891 ms  Scan sent (load_directory)10:28:44.165
    + 2034 ms  Scan acknowledged         10:28:44.308

  Time-till-Nth thumbnail — warm cache:
      N   from GUI launch   from scan sent  filename
      1          2254 ms          363 ms  ← first image
      5          2341 ms          450 ms
     10          2478 ms          587 ms

  Thumbnails received : 2225
  Throughput          : 42.3 / s
  Inter-notif gaps (first 99):
    mean 23.6 ms  median 18.4 ms  stdev 31.2 ms  max 312.0 ms
  Session duration    : 47.3 s  (exit code 0)
```

**Limitations:** The scraper reads log timestamps which have 1 ms resolution; it cannot distinguish between events that happen within the same millisecond. The "from GUI launch" column includes daemon startup time for the first session in a daemon lifetime.

---

### 4.3 `bench_thumbnail.py` — per-file micro-benchmark

Measures each sub-step of the CR3 thumbnail pipeline in isolation, on actual files, without the daemon.

```bash
python3 bench_thumbnail.py /path/to/cr3/folder [--count 20]
```

**What it measures per file:**
- `read`: NAS read time for 512 KB header
- `buf`: time to scan the buffer for the Canon uuid thumbnail
- `et`: exiftool stay-open call time (only when buffer miss)
- `pil`: PIL decode + LANCZOS resize + JPEG save

**Example output:**
```
[ 1/20] _MG_7485.CR3       read= 847.3ms  buf= 0.04ms  et=   0.0ms  pil=  8.4ms  total= 855.7ms  [buffer]
[ 2/20] _MG_6957.CR3       read= 923.1ms  buf= 0.03ms  et=   0.0ms  pil=  7.9ms  total= 931.0ms  [buffer]

Summary (20 files):
  Buffer hits : 18/20 (90%)
  Total/file  : mean=891.2ms  median=873.4ms  stdev=112.3ms  max=1234.5ms
  NAS read    : mean=840.1ms  median=821.3ms  max=1198.2ms
  Throughput  : ~1.1 files/s single-thread  (~9 with 8 workers)
```

Buffer hit rate directly predicts whether a library can be thumbnailed without exiftool per-file calls. A low hit rate (< 80%) typically indicates CR3 files with unusually large metadata blocks.

---

### 4.4 `benchmarks/plugin_benchmarks.py` — cold/warm/parallel comparison

Older benchmark (German comments) that runs cold, warm, and parallel processing of a single CR3 file to compare first-run vs. cache hit vs. concurrency scaling. Useful as a quick sanity check when changing plugin code.

```bash
python3 benchmarks/plugin_benchmarks.py
```

Requires editing `TEST_CR3_FILE` to point to an actual file.

---

### 4.5 Structured startup logs

The GUI emits timing markers at each pipeline milestone, relative to a `perf_counter` origin set when `load_directory` is called:

```bash
grep "\[startup\]" image_viewer.log | tail -10
```

```
10:23:45,012 [INFO] ... [startup] load_directory called for /Users/joe/Pictures
10:23:45,087 [INFO] ... [startup] first scan_progress: 75 ms (50 files)
10:23:45,923 [INFO] ... [startup] scan_complete: 911 ms
10:23:45,940 [INFO] ... [startup] first previews_ready: 928 ms
```

These are the cheapest way to detect a regression — add one line to the pipeline, re-run, compare the four numbers.

---

### 4.6 Regression table

| Date | Commit | t_first_image | Notes |
|---|---|---|---|
| 2026-02-20 | `0b82590` | ~1,700 ms | Cold cache baseline (thumbnails generating) |
| 2026-02-20 | `0438cfa` | ~3,500 ms | `previews_ready` threshold lowered to `GUI_REQUEST_LOW`; slow scan notified in FS order |
| 2026-02-20 | `34e7655` | **< 200 ms** | `request_thumbnail` fast path: cached files bypass task graph |
| 2026-02-20 | `468e49f` | TBD | Fast scan moved to dedicated OS thread; window shown before load_directory |
