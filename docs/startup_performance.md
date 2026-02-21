# Startup Performance — Time to First Image

## The Startup Pipeline

From GUI launch to first rendered thumbnail, six stages run in sequence:

```
1. Daemon ready check          GUI polls socket until daemon creates it
2. get_directory_files         Synchronous DB query; returns cached file list immediately
3. fast_scan_job (GUI_REQUEST) Populates placeholders; fires scan_progress per batch
4. _prioritize_visible         GUI identifies viewport-visible paths, calls request_previews
5. request_thumbnail (daemon)  Per visible path: cached → previews_ready; in-flight → priority upgrade
6. previews_ready → paint      GUI loads QImage from disk, updates label
```

Stages 3–6 overlap. The fast scan and viewport prioritization run concurrently as placeholders appear.

---

## Measured Timings (2,225-file library, ~/Pictures recursive)

| Milestone | Warm cache | Generating thumbnails |
|---|---|---|
| `load_directory` called | 0 ms | 0 ms |
| First `scan_progress` | ~75 ms | ~75 ms |
| `scan_complete` (fast scan) | ~900 ms | ~900 ms |
| First `previews_ready` | **< 200 ms** ¹ | ~1,700 ms ² |

¹ After `request_thumbnail` fast-path fix — one socket round-trip + DB lookup per visible file.
² Depends on thumbnail generation speed (exiftool + PIL); parallelised across 8 workers.

---

## The Two-Job Scan Design

Two `SourceJob`s run concurrently after a `get_directory_files` request:

**fast_scan_job** (`GUI_REQUEST`, `create_tasks=False`)
Scans the directory and emits `scan_progress` notifications with file paths. The GUI creates black placeholder labels as batches arrive. No backend tasks are submitted.

**slow_scan_job** (`GUI_REQUEST_LOW`, `create_tasks=True`)
Scans the same directory and calls `create_tasks_for_file` for each path:
- Thumbnail **already cached** → emits `previews_ready` immediately (no task submitted)
- Thumbnail **not cached** → submits `meta::path` + `path` tasks with correct dependency

The fast scan runs at higher priority and completes first, giving the GUI a full placeholder grid before any thumbnails arrive.

---

## Viewport Prioritisation

After `scan_complete` (and on every scroll/resize, throttled to ~150 ms), the GUI calls `_prioritize_visible_thumbnails`:

1. Calculates visible row range from the layout manager
2. Collects paths of unloaded visible labels
3. Sends `request_previews(paths, GUI_REQUEST)` to the daemon

The daemon handles each path in `request_thumbnail`:

```
is_thumbnail_valid(path)?
  YES → get_thumbnail_paths() → put previews_ready on notification_queue   (fast path, < 1 ms)
  NO  → update_task_priorities({meta::path, path}, GUI_REQUEST)             (priority upgrade)
```

The fast path is the dominant case once the library has been scanned at least once. It bypasses the task graph entirely and responds in one DB read.

---

## Known Bottlenecks

**Slow scan sequential order**
The slow scan processes files in filesystem order. Off-screen files receive `previews_ready` in that order, not viewport order. The viewport prioritisation compensates for visible files, but users who scroll quickly may outpace the slow scan for uncached libraries.

**First-run generation**
On a cold cache, thumbnail generation time dominates (~1.5–3 s for the first visible batch, depending on CPU and file format). CR3 files require exiftool extraction; JPEGs are significantly faster via PIL.

---

## Regression History

| Date | Commit | Time to first image | Notes |
|---|---|---|---|
| 2026-02-20 | `0b82590` | ~1.7 s | Thumbnails being generated fresh |
| 2026-02-20 | `0438cfa` | ~3.5 s | `previews_ready` threshold lowered to `GUI_REQUEST_LOW`; slow scan now sends notifications for all cached files in filesystem order |
| 2026-02-20 | `34e7655` | **< 200 ms** | `request_thumbnail` fast path: cached files emit `previews_ready` immediately on viewport request |
| 2026-02-20 | `(current)` | TBD | Fast scan batched to 50 files/slice; `[startup]` timing added to log |

---

## Profiling Guidance

Structured timing is now emitted by the GUI at each pipeline milestone. Read it with:

```bash
grep "\[startup\]" image_viewer.log | tail -10
```

Example output:
```
2026-02-20 10:23:45,012 [INFO] ... [startup] load_directory called for /Users/joe/Pictures
2026-02-20 10:23:45,087 [INFO] ... [startup] first scan_progress: 75 ms after load_directory (50 files in batch)
2026-02-20 10:23:45,923 [INFO] ... [startup] scan_complete: 911 ms after load_directory
2026-02-20 10:23:45,940 [INFO] ... [startup] first previews_ready: 928 ms after load_directory
```

All four values are relative to the same `perf_counter` origin set at `load_directory`, so no manual timestamp subtraction is needed.

Key deltas:
- `load_directory` → first `scan_progress`: socket latency + fast scan start (~75 ms expected)
- `load_directory` → `scan_complete`: full directory traversal time
- `scan_complete` → first `previews_ready`: viewport prioritisation round-trip (~150 ms timer + socket + DB; ~0 ms warm cache via fast path)
