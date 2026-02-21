# Inspector View — Tracking and Navigation Behaviour

This document captures the precise tracking and navigation behaviour of `InspectorView` (`gui/inspector_view.py`) so it can be faithfully reconstructed after a regression.

---

## Overview

The Inspector is a floating, independently-resizable window (`Qt.Window`) that displays a magnified portion of the currently hovered image. It has three mutually exclusive modes — **Tracking**, **Fit**, and **Locked** — controlled by two boolean flags: `_is_fit_mode` and `_is_manual_mode`. The flags are never both `True` simultaneously.

---

## Modes

### Tracking mode (default)

**Window title:** `Inspector - Tracking (Nx Zoom)`
**Flags:** `_is_fit_mode = False`, `_is_manual_mode = False`

This is the default and primary mode. The inspector viewport centre is driven entirely by incoming `INSPECTOR_UPDATE` events — wherever the mouse is on the source image, the inspector magnifies that exact point.

**On every `INSPECTOR_UPDATE` event (same image, already loaded):**
- `set_center(normalized_position)` is called immediately, moving the viewport centre to the cursor's normalised coordinate.
- No socket call is made (fast path).

**On an image change:**
- The new view image is loaded from disk.
- `setZoom(_zoom_factor)` is called on `PictureBase` — the tracked zoom value is restored/applied.
- `set_center(normalized_position)` is called at the end of `update_view`.

**Zoom level:** the persisted `_zoom_factor`. Default `3.0`, range `[0.1, 20.0]`, saved to `config.yaml` under `inspector.zoom_factor`.

**Interaction:** left-drag panning and right-drag zoom are both enabled.

---

### Fit mode

**Window title:** `Inspector - Fit Mode`
**Flags:** `_is_fit_mode = True`, `_is_manual_mode = False`

The image is always scaled to fill the inspector window. The viewport centre is locked to `(0.5, 0.5)`. Tracking is fully suspended.

**On every `INSPECTOR_UPDATE` event (same image):**
- The fast path returns immediately without calling `set_center` — nothing moves.

**On an image change:**
- `update_view` is called with a hardcoded `norm_pos = QPointF(0.5, 0.5)`.
- After loading the new image, `setFitMode(True)` is called on `PictureBase`, which recalculates zoom to fit and centres at `(0.5, 0.5)`.
- `set_center` is **not** called because `_is_fit_mode` blocks it.

**On window resize:**
- `resizeEvent` calls `setFitMode(True)` again so the image re-fits to the new window size.

**Interaction:** left-drag panning and right-drag zoom are both **blocked** — `mousePressEvent` and `mouseMoveEvent` return early when `_is_fit_mode` is `True`.

**Scroll wheel in fit mode:** exits fit mode first (sets `_is_fit_mode = False`, calls `setFitMode(False)` and `setZoom(_zoom_factor)`, updates title), then applies the wheel zoom step. The mode becomes whichever was last active before fit — but note the mode flags are not saved across the fit transition, so after scroll wheel the inspector ends up in **Tracking** state (both flags `False`).

---

### Locked mode

**Window title:** `Inspector - Locked (Nx Zoom)`
**Flags:** `_is_fit_mode = False`, `_is_manual_mode = True`

The inspector freezes at the zoom and centre that were active when locked mode was entered. Tracking is suspended; panning is still possible.

**On every `INSPECTOR_UPDATE` event (same image):**
- The fast path returns immediately without calling `set_center` — nothing moves.

**On an image change:**
- `update_view` is called with a hardcoded `norm_pos = QPointF(0.5, 0.5)`.
- After loading the new image, `setZoom(_zoom_factor)` is called (locked mode does not use `setFitMode`).
- `set_center` is **not** called because `_is_manual_mode` blocks it — the view centres on `(0.5, 0.5)` only because `_zoom_factor` is applied fresh on the new image; the user's manually panned position is not preserved across image changes.

**Interaction:** left-drag panning and right-drag zoom are both enabled. Panning calls `_picture_base.setCenter()` directly; it does not exit locked mode.

---

## Mode Transitions — double-click (left button)

```
Tracking  ──dbl-click──▶  Fit  ──dbl-click──▶  Locked  ──dbl-click──▶  Tracking
```

| From     | To       | Exact side effects                                                              |
|----------|----------|---------------------------------------------------------------------------------|
| Tracking | Fit      | `_is_fit_mode = True` · `_is_manual_mode = False` · `setFitMode(True)`         |
| Fit      | Locked   | `_is_fit_mode = False` · `_is_manual_mode = True` · `setFitMode(False)` — **no zoom call**, zoom stays at whatever fit calculated |
| Locked   | Tracking | `_is_fit_mode = False` · `_is_manual_mode = False` · `setFitMode(False)` · `setZoom(_zoom_factor)` |

`_update_window_title()` is called at the end of every transition.

> **Critical detail — Fit → Locked:** when leaving fit mode via double-click, `setFitMode(False)` is called but `setZoom` is **not**. `PictureBase.setFitMode(False)` only clears the `fit_mode` flag; it does not snap zoom to any value. The zoom visible in locked mode is therefore the auto-fit zoom that was in effect, not `_zoom_factor`.

> **Critical detail — Locked → Tracking:** `setZoom(_zoom_factor)` is explicitly called, restoring the pre-fit inspector zoom. This is the only transition that re-applies `_zoom_factor` to `PictureBase`.

---

## Zoom (`_zoom_factor`)

`_zoom_factor` is the inspector's **own** zoom register. It is separate from `PictureBase`'s internal zoom state.

- **Default:** `3.0` (loaded from `config.yaml` at startup via `config_manager.get("inspector.zoom_factor", 3.0)`).
- **Applied to `PictureBase`** only when: entering tracking mode from locked, loading a new image in tracking or locked mode, or when scroll-wheel zoom calls `set_zoom_factor`.
- **Not applied** when entering locked mode from fit (deliberate — preserves the fit-calculated zoom).
- **Clamped** to `[0.1, 20.0]` inside `set_zoom_factor`.
- **Saved** to `config.yaml` by `set_zoom_factor` on every change, and also in `closeEvent`.

### Scroll wheel zoom
```
factor = 1.25  (scroll up)  or  1/1.25  (scroll down)
new_zoom = _zoom_factor * factor
```
If currently in fit mode, the wheel first exits fit mode (title updates to Tracking), then applies the step. The mode ends up in Tracking (both flags `False`).

### Right-drag zoom
- Dead zone: ±10 px horizontal movement required before zoom changes.
- Formula: `new_zoom = initial_zoom * (1.0 + (delta_x − 10) / 100.0)`
- Calls `set_zoom_factor`, which updates `_zoom_factor`, saves to config, and calls `setZoom` on `PictureBase`.
- Blocked entirely in fit mode.

---

## INSPECTOR_UPDATE Event Handling (`_handle_inspector_update`)

This is the core of the tracking system. The event carries `image_path` (original full-size path) and `normalized_position` (0–1 in both axes, matching the padded-square coordinate system of `PictureBase`).

**Sources that publish `INSPECTOR_UPDATE`:**
- `ThumbnailView` (`gui/thumbnail_view.py`) — mouse-move over a thumbnail label
- `PictureView` (`gui/picture_view.py`) — mouse-move over the full image view, and also on image load (publishes centre `(0.5, 0.5)`)
- `MainWindow` (`gui/main_window.py`) — additional mouse-move handling at window level
- `gui/components/event_handler.py` — component-level mouse routing

**Processing logic:**

1. **Guard**: if the inspector window is not visible, return immediately.
2. **Same-image fast path**: if `image_path == _current_image_path` and `_view_image_ready is True`, skip the socket call entirely. If also not in fit or manual mode, call `set_center(normalized_position)` and return. This is the hot path during normal cursor tracking.
3. **Image-changed path**: set `_view_image_ready = False`.
4. **Socket call** (`get_previews_status`): query the daemon for the view-image file path (the full-resolution version cached to disk).
   - If the view image is **ready and the file exists on disk**: call `update_view(image_path, view_image_path, normalized_position)`.
   - If the view image is **not ready**: if this is a new image, clear the display (`setImage(QImage())`) and update `_current_image_path`; then call `request_previews([image_path])` to trigger generation, and return (the daemon will send a `previews_ready` notification later).

---

## Daemon Notification Handling (`_on_daemon_notification`)

When the daemon finishes generating the view image it sends a `previews_ready` notification. `InspectorView` subscribes to `EventType.DAEMON_NOTIFICATION` and processes it here.

- If `notification.image_path == _current_image_path` and `view_image_path` is set, `_picture_base.loadImageFromPath(view_image_path)` is called immediately and `_view_image_ready` is set to `True`.
- There is **no position update** here — the next `INSPECTOR_UPDATE` event (next cursor move) will trigger the fast path and call `set_center()`.

---

## `update_view` Method

Called when the view image is confirmed ready on disk.

1. If the image changed (`original_image_path != _current_image_path`):
   - Load via `_picture_base.loadImageFromPath(view_image_path)`.
   - On success: set `_current_image_path`, set `_view_image_ready = True`, call `setViewportSize(self.size())`, then apply the correct zoom mode:
     - Fit mode → `setFitMode(True)`
     - Otherwise → `setZoom(_zoom_factor)`
   - On failure: set `_current_image_path = None` and return.
2. If not in fit or manual mode, call `set_center(norm_pos)`.

---

## Mouse Interaction

### Left-button drag — panning

- Not available in fit mode.
- `mousePressEvent`: start panning, record `_last_mouse_pos`, set closed-hand cursor.
- `mouseMoveEvent` while panning: compute pixel delta → invert the current transform to get normalised delta → update `_picture_base` centre directly via `setCenter()`. **Does not go through the event system.**
- `mouseReleaseEvent`: stop panning, restore arrow cursor.
- Panning does **not** set `_is_manual_mode`; the inspector can return to tracking on the next image-change if the user double-clicks back to tracking mode.

### Right-button drag — drag zoom

- Not available in fit mode.
- `mousePressEvent`: calls `_picture_base.startDragZoom(anchor, start_pos)` where `anchor` is `screenToNormalized(event.position())`.
- `mouseMoveEvent`: if `_picture_base.isDragZooming()` is True, computes horizontal delta, applies a 10px dead-zone threshold, then calls `set_zoom_factor(new_zoom)`.
  - Zoom formula: `new_zoom = initial_zoom * (1.0 + (delta_x - threshold) / 100.0)`
- `mouseReleaseEvent`: calls `_picture_base.endDragZoom()`.

### Scroll wheel — zoom

- If currently in fit mode, first exits fit mode (`setFitMode(False)`, `setZoom(_zoom_factor)`) then applies the wheel step.
- Zoom step: factor 1.25 per wheel click (scroll up = zoom in, scroll down = zoom out).
- Calls `set_zoom_factor(_zoom_factor * factor)`.
- `set_zoom_factor` clamps to [0.1, 20.0], saves to config, calls `_picture_base.setZoom()`.

---

## Coordinate System

All positions are in the **padded-square normalised space** defined by `PictureBase`:

- The image is embedded in a square whose side is `max(image_width, image_height)`.
- Normalised `(0, 0)` = top-left of the padded square; `(1, 1)` = bottom-right.
- Y-axis is **flipped** in the transform: `norm_y = 1.0 - ((padded_y - rect.top) / rect.height)`.
- `(0.5, 0.5)` always refers to the true centre of the padded square, which coincides with the centre of the image.

---

## Persistence

| Item                  | Storage                                              | Key                        |
|-----------------------|------------------------------------------------------|----------------------------|
| Window geometry       | `QSettings("RabbitViewer", "Inspector")`             | `"geometry"`               |
| Zoom factor           | `config.yaml` via `ConfigManager`                    | `"inspector.zoom_factor"`  |

Both are saved in `closeEvent`. Window geometry is restored in `__init__`; zoom factor is loaded and applied to `PictureBase` in `__init__` as well.

---

## Invariants to Preserve

1. `_view_image_ready` must be `False` whenever `_current_image_path` changes; it is only set `True` after a successful `loadImageFromPath`.
2. In fit mode, `set_center()` is **never** called; the centre is always `(0.5, 0.5)`.
3. In locked mode, `set_center()` is **never** called on incoming events; it may be moved by panning.
4. Leaving fit mode via double-click (→ locked) does **not** call `setZoom`; the zoom stays at whatever fit calculated.
5. Leaving locked mode via double-click (→ tracking) calls `setZoom(_zoom_factor)` to restore the pre-lock zoom.
6. Scroll wheel always exits fit mode before applying the new zoom step.
7. The socket call (`get_previews_status`) is only made when `_view_image_ready` is `False`; the fast path must bypass it completely to avoid UI stalls.
