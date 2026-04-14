# Debugging Protocol

A step-by-step workflow for diagnosing issues in RabbitViewer. Prioritize **logging first, breakpoints second** — instrument before you inspect.

---

## Step 0: Make the App Fail Loudly

Before investigating any bug, make sure these safeguards are active. They turn silent failures into immediate, visible errors — cutting the time between "something is wrong" and "here is the stack trace."

### Route Qt C++ warnings to the Python logger

Qt's C++ layer whispers complaints to stderr (e.g. `QPixmap: Invalid image`, layout constraint errors) long before a bug manifests in Python. Route them into the unified log so nothing is missed:

```python
from PySide6.QtCore import qInstallMessageHandler, QtMsgType
import logging

logger = logging.getLogger("qt_core")

def qt_message_handler(mode, context, message):
    if mode == QtMsgType.QtInfoMsg:
        logger.info(message)
    elif mode == QtMsgType.QtWarningMsg:
        logger.warning(message)
    elif mode == QtMsgType.QtCriticalMsg:
        logger.error(message)
    elif mode == QtMsgType.QtFatalMsg:
        logger.critical(message)

# Call this right after creating QApplication
qInstallMessageHandler(qt_message_handler)
```

### Kill silent slot exceptions (`sys.excepthook`)

In PySide6, exceptions inside signal/slot connections often just print a traceback to the console while the app keeps running in a zombified state. Force them into the log:

```python
import sys
import logging

logger = logging.getLogger("crash_reporter")

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

# Set the hook before starting the event loop
sys.excepthook = handle_exception
```

### Visual layout debugging (`--debug-ui`)

Half the bugs in an image viewer are visual: a thumbnail grid sizing incorrectly, an image scaling to 0x0 and disappearing. Instead of guessing math, use a flag that applies loud borders to all widgets:

```python
if debug_ui_mode:
    main_window.setStyleSheet("QWidget { border: 1px solid red; }")
```

Launch with `python main.py --debug-ui [directory]` to instantly see every widget boundary.

---

## Step 1: Reproduce the Bug

- Get a **minimal, reliable reproduction** before touching any code.
- Note the exact steps, directory contents, and file types involved.
- Check if the issue occurs in GUI mode, daemon mode, or both.

## Step 2: Check Existing Logs

Logs live at `~/.rabbitviewer/` (50 MB per file, 3 rotated backups):

- **GUI**: `~/.rabbitviewer/rabbitviewer.log`
- **Daemon**: `~/.rabbitviewer/daemon.log`

```bash
# Tail live output (GUI)
tail -f ~/.rabbitviewer/rabbitviewer.log

# Tail daemon log
tail -f ~/.rabbitviewer/daemon.log

# Search for errors/warnings
grep -E '\[(ERROR|WARNING)\]' ~/.rabbitviewer/rabbitviewer.log | tail -50
```

**For AI-agent use**, `rabbit grep-logs` wraps the above with sane defaults:

```bash
# Errors + warnings from both logs (last 200 lines each)
rabbit grep-logs

# Scan more history
rabbit grep-logs --tail 1000

# Add custom search terms on top of the default level filter
rabbit grep-logs --pattern "thumbnail" --pattern "cache miss"

# Show surrounding context lines
rabbit grep-logs --context 3

# Lower the level floor to INFO; daemon log only
rabbit grep-logs --level INFO --daemon-only

# Pure keyword search, no level filter
rabbit grep-logs --no-defaults --pattern "speculative"
```

Run `rabbit grep-logs --help` for all options.

Default format: `%(asctime)s [%(levelname)s] %(name)s - %(message)s`

Every module uses `logger = logging.getLogger(__name__)`, so `%(name)s` tells you exactly where the message originated (e.g. `core.rendermanager`, `gui.thumbnail_view`).

## Step 3: Increase Log Verbosity

### Quick override (CLI)

For a single debugging session, use the `--log-level` flag — no config changes needed:

```bash
python main.py --log-level DEBUG [directory]
python main.py --daemon --log-level DEBUG
```

### Global level (config)

In `~/.config/rabbitviewer/config.yaml`:

```yaml
logging_level: DEBUG
```

### Per-module overrides

Narrow the noise by targeting specific modules:

```yaml
logging_level: INFO                 # keep global at INFO
logging_levels:
  core.rendermanager: DEBUG         # verbose render pipeline
  core.metadata_database: DEBUG     # every DB query
  gui.thumbnail_view: DEBUG         # grid layout decisions
  plugins.cr3_plugin: DEBUG         # RAW decode path
```

Module names match Python package paths. Any module that calls `logging.getLogger(__name__)` can be targeted.

### Restart required

Log levels are applied at startup in `setup_logging()`. Restart the GUI or daemon after changing config. The `--log-level` CLI flag overrides `logging_level` from config but does not affect `logging_levels` per-module overrides (both are applied).

## Step 4: Add Targeted Log Statements

If existing logs don't reveal the problem, **add temporary log lines** at suspected fault points. Follow these conventions:

```python
# Use the module-level logger, never print()
logger.debug("description: key=%s value=%r", key, value)
```

**Rules:**

- Prefer `%s`/`%r` formatting in DEBUG calls inside hot loops — lazy evaluation skips string formatting when DEBUG is disabled. f-strings are fine everywhere else; the performance difference is negligible outside tight loops.
- Use `%r` for values that might be `None`, empty, or contain surprising types.
- Log **inputs and outputs** of the suspected function, not just "entered function X".
- Include identifiers that let you correlate across threads: job IDs (`gui_scan::`, `daemon_idx::`), file paths, task states.
- For threading issues, add `threading.current_thread().name` to pinpoint which worker is involved.

```python
logger.debug("process_task: job=%s path=%s thread=%s",
             task.job_id, task.path, threading.current_thread().name)
```

### Key instrumentation points by subsystem

| Subsystem | Key locations |
|---|---|
| Scan pipeline | `DirectoryScanner.scan()`, `RenderManager._cooperative_generator_runner()` |
| Task lifecycle | `RenderManager._process_task()`, `RenderManager.cancel_task()` |
| Thumbnail cache | `ThumbnailManager.get_thumbnail()`, `MetadataDatabase.get_cached_thumbnail_paths()` |
| GUI rendering | `ThumbnailView.paint_cell()`, `PictureView._display_image()` |
| Plugin decode | `BasePlugin.get_thumbnail()`, `BasePlugin.get_metadata()` |
| File watcher | `WatchdogHandler.on_modified()`, `WatchdogHandler.on_created()` |
| Event system | `EventSystem.emit()`, `EventSystem.subscribe()` |

## Step 5: Use Breakpoints for State Inspection

When logs narrow the problem to a specific code path but you need to inspect live state (locals, object graphs, thread stacks), switch to breakpoints.

### Option A: `breakpoint()` (built-in pdb)

```python
def suspect_function(self, task):
    # Conditional breakpoint - only stops on the interesting case
    if task.state == TaskState.FAILED:
        breakpoint()
```

Run with: `python main.py [directory]` (pdb attaches to stdin/stdout).

**pdb quick reference:**

| Command | Action |
|---|---|
| `n` | Next line |
| `s` | Step into |
| `c` | Continue |
| `p expr` | Print expression |
| `pp vars(obj)` | Pretty-print object attributes |
| `w` | Show call stack |
| `l` | List source around current line |
| `threading.enumerate()` | List all live threads |

### Option B: IDE debugger (VS Code / PyCharm)

For GUI issues, an IDE debugger is often easier than pdb because Qt's event loop makes stdin-based debugging awkward.

1. Set a breakpoint in the IDE at the suspected line.
2. Launch via the IDE's debug configuration (point it at `main.py`).
3. Use the variables panel to inspect `self`, locals, and thread state.

### Option C: Post-mortem debugging

For crashes, enable post-mortem automatically:

```bash
python -m pdb main.py [directory]
# pdb will drop you into the frame where the exception occurred
```

## Step 6: Isolate Threading Issues

RabbitViewer is heavily multi-threaded (RenderManager workers, watchdog, Qt main thread). Threading bugs require extra care:

1. **Log thread identity** at every suspected contention point:
   ```python
   logger.debug("acquire lock: caller=%s", threading.current_thread().name)
   ```

2. **Dump all thread stacks** when the app hangs:
   ```python
   import traceback, sys
   for thread_id, frame in sys._current_frames().items():
       print(f"\n--- Thread {thread_id} ---")
       traceback.print_stack(frame)
   ```
   Bind this to a signal for on-demand use:
   ```python
   import signal
   signal.signal(signal.SIGUSR1, lambda *_: [
       traceback.print_stack(f) for f in sys._current_frames().values()
   ])
   ```
   Then: `kill -USR1 <pid>`

3. **Check for priority inversion**: If a high-priority task is waiting, log the queue state:
   ```python
   logger.debug("queue size=%d, top priority=%s", rm._task_queue.qsize(), ...)
   ```

## Step 7: Verify the Fix

1. Confirm the original reproduction case passes.
2. Run the test suite: `pytest tests/ -q`
3. **Remove all temporary log lines and breakpoints** before committing. Only keep log lines that have lasting diagnostic value (error paths, major state transitions).

## Step 8: Decide What Logging to Keep

Permanent log lines should cover:

- **Error paths** — always log at `ERROR` or `WARNING` with context (file path, task ID, exception).
- **Major state transitions** — job start/complete, scan phases, daemon lock acquire/release at `INFO`.
- **Hot-path diagnostics** — task scheduling, cache hits/misses, heatmap updates at `DEBUG` (hidden by default).

Never log at `DEBUG` in tight loops without a guard — even lazy formatting has overhead at thousands of calls per second.

---

## Quick Reference

| Situation | First action |
|---|---|
| Crash / traceback | Read the log, run `python -m pdb main.py` |
| Wrong output / visual glitch | Add DEBUG logging to the rendering path |
| Hang / freeze | `kill -USR1 <pid>` to dump thread stacks |
| Slow performance | Profile first (`py-spy`), then log timing |
| Race condition | Log thread names + lock acquire/release |
| Daemon not indexing | Check `~/.rabbitviewer/gui.lock` ownership, `tail -f ~/.rabbitviewer/daemon.log` |
| Silent GUI glitch | Run with `QT_FATAL_WARNINGS=1 python main.py` to crash on Qt warnings, then use pdb |
