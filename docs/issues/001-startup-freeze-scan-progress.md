# 001 — GUI freezes for ~40s on directory load

**Status:** fixed
**File:** `gui/thumbnail_view.py` — `_add_image_batch()`, `_process_daemon_notification()`

## Observed behaviour

When opening a directory with ~2200 files the app is completely unresponsive for ~40 seconds before any thumbnails appear.

Log evidence (`image_viewer.log`):
```
10:10:02,256 — first ThumbnailLabel created
10:10:42,718 — last ThumbnailLabel created   (~40s later)
```
1767 `ThumbnailLabel created` log entries, one per `scan_progress` notification.

## Desired behaviour

The window should appear and be interactive immediately. Thumbnails for files already cached in the database should appear within a second. New files should stream in progressively without blocking the UI.

## Root cause

The daemon sends `scan_progress` notifications one file at a time. For each notification the GUI handler calls:

```
_process_daemon_notification()
  → _add_image_batch([one_file])
      → _reapply_filters_and_update_layout()   ← full layout rebuild
```

`_reapply_filters_and_update_layout()` re-queries the daemon and redraws the entire grid on every single file. With 2227 files this is **2227 full layout rebuilds on the GUI thread**, holding the event loop hostage for the entire duration.

## Fix direction

Debounce `_reapply_filters_and_update_layout()` inside `_add_image_batch()`. Files should accumulate in `self.all_files` as notifications arrive; the layout rebuild should fire at most once per ~100 ms via a coalescing `QTimer`. The per-file call to `_reapply_filters_and_update_layout()` at line 738 is the specific line to change.
