# Video Hover Alignment — Diagnosis and Fix

## Symptom

When hovering a video thumbnail in the grid, the first frame rendered by mpv
appears shifted by 1–2 pixels toward the top-left relative to the cached JPEG
thumbnail. Visually: the image "jumps" slightly as hover begins.

Also observed: the colour of the hover frame sometimes looks subtly different
from the thumbnail even without any shift.

## Investigation — what didn't fix it

We followed three hypotheses, two of which turned out to be wrong. Documenting
them is useful because the instincts behind each were reasonable but lacked
data to confirm.

### Hypothesis A — different renderers for thumb vs hover

**Idea:** the thumbnail went through `ffmpeg -vf scale` → MJPEG encoder; the
hover went through libmpv's OpenGL video output. Different scalers, different
colour math, different first-frame content.

**Action:** migrated `plugins/video_plugin.py` from ffmpeg CLI to mpv CLI, so
both paths share the same decoder + swscale chain. Bumped
`VideoPlugin.cache_version = 2` to invalidate old ffmpeg-era cache entries.
Added `--hr-seek=yes` to the mpv command (later bumped to v3) so the cached
frame matches the hover's precise seek.

**Result:** the frame *content* matches now, but a 1–2 px positional jump
remained.

### Hypothesis B — `keepaspect=no` stretch divergence

**Idea:** the scrub-mode mpv used `keepaspect="no"` to fill the widget by
stretching. For non-square widgets this could diverge from ffmpeg's letterbox
by a pixel.

**Action:** changed `gui/video_view.py:117` to `keepaspect="yes"`.

**Result:** inert for the test file (square 1024×1024) — both flags produce
identical output when widget aspect == video aspect. Did not affect the jump.

### Hypothesis C — `_pixmap_rect_in_label` returned the wrong rect

**Idea:** `_pixmap_rect_in_label` returned the aligned pixmap rect, which for
a 128×128 pixmap in a 124×124 `contentsRect` expands to `(0, 0, 128, 128)` —
covering the border. We modified it to clip via `aligned.intersected(content)`
so the video widget matches the JPEG's visible area.

**Result:** made things *worse*. The intersection shrank the widget to
`(2, 2, 124, 124)`, which then triggered independent mpv letterboxing inside a
smaller rect — producing a new aspect mismatch and showing a different crop
of the source video than the JPEG. Reverted.

## Building the diagnostic tool

At this point we'd made three guesses with no pixel-level data to distinguish
them. The next step was instrumentation, not another guess.

`scripts/diff_video_alignment.py` runs two tests side by side and reports the
pixel-level shift between thumbnail and hover:

1. **Isolated** — a 128×128 `VideoView` with no label, no border. Measures
   raw pipeline divergence (the inherent ffmpeg-vs-mpv-GL render noise).
2. **Production replica** — an `ItemCard` with its real stylesheet border, a
   `VideoView` positioned via the same `_pixmap_rect_in_label` logic the grid
   uses, both grabbed and composited. Reports the shift the user actually
   sees.

Key tool choices:

- `QOpenGLWidget.grabFramebuffer()` — returns the exact pixels libmpv rendered
  to the FBO. Must be preceded by `view.repaint()` (synchronous) not
  `view.update()` (async) or the grab races the paint and you get black.
- Brute-force MSE shift search over ±6 px, so "jump by N pixels" becomes a
  concrete integer. No scipy/scikit-image dependency.
- Output: `/tmp/align_*.png` for eyeballing, plus MSE numbers printed.

Each run produces numbers like:

```
Best shift: dy=+0, dx=+0   MSE=10.34
```

Once we had numbers, the investigation stopped being guessing.

## Root cause

The production replica reported `MSE=171` at zero shift with *no* integer
offset. Splitting the diff by region:

```
outer 2-px ring MSE  : 2641.99   ← huge
inner 124×124 MSE    :   92.35   ← small (just renderer noise)
jpeg (0,0)  = (26, 26, 26)       ← #1a1a1a card background
hover (0,0) = (255, 24, 0)       ← red marker corner from the video
```

The mismatch was entirely in the outermost 2-pixel ring. Inside 124×124,
thumb and hover agreed to within renderer noise.

**Why the ring mismatches:**

- `ItemCard` has a 2 px stylesheet border (`border: 2px solid transparent`),
  so its `contentsRect` is `(2, 2, 124, 124)`.
- `QLabel` paints a pixmap using `QStyle.alignedRect(AlignCenter, pix.size(),
  contentsRect)`. A 128×128 pixmap in a 124×124 content rect returns
  `(0, 0, 128, 128)` — the pixmap's natural size centered, extending 2 px
  past `contentsRect` on every edge. **QLabel then clips the paint to
  `contentsRect`**, hiding the outer 2 px of the pixmap behind the border.
- The `VideoView` overlay was sized to that same `(0, 0, 128, 128)` aligned
  rect. But `VideoView` is a `QOpenGLWidget` with *no* stylesheet border, so
  its paint is **not** clipped. It renders video content in the full 128×128
  area — including the 2 px ring that was hidden-by-border in the JPEG
  state.

On hover, the user sees 2 pixels of video content suddenly *appear* on every
edge where a solid background colour was before. The brain reads that as a
1–2 px shift toward the top-left (attention latches onto the new content
first appearing there).

## Fix

Override `ItemCard.setPixmap()` to scale the incoming pixmap to fit inside
`contentsRect` with aspect-ratio preservation, so the pixmap never extends
past where `QLabel` will clip it. The `_pixmap_rect_in_label` computation
then naturally returns the inset rect, and the `VideoView` overlay lands
exactly where the JPEG was visible.

`gui/components/item_card.py`:

```python
def setPixmap(self, pixmap: QPixmap) -> None:
    if not pixmap.isNull():
        content = self.contentsRect()
        cw, ch = content.width(), content.height()
        if cw > 0 and ch > 0 and (pixmap.width() > cw or pixmap.height() > ch):
            pixmap = pixmap.scaled(
                cw, ch,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    super().setPixmap(pixmap)
```

One method override, ~10 lines, no changes to `_pixmap_rect_in_label` (we
reverted the intersection attempt from hypothesis C) and no changes to the
cache or thumbnail generation. Because the override only downscales pixmaps
that exceed `contentsRect` (≤ 4 px delta at a 128-size card), the additional
Qt bilinear scale is imperceptible.

Why not `setScaledContents(True)`? That flag does stretch-to-fit without
preserving aspect ratio, so a 16:9 video's 128×72 thumbnail would be stretched
to 124×124 and look squashed. `scaled(..., KeepAspectRatio)` is what we want.

## Verification

Diagnostic output from `scripts/diff_video_alignment.py` on the marker video:

| State | Zero-shift MSE | Outer-ring MSE | Inner MSE |
|---|---:|---:|---:|
| Before fix | 171.40 | 2641.99 | 92.35 |
| After fix | 10.34 | 0.00 | 11.02 |

The outer ring MSE dropped from 2642 to **0** (pixel-identical border
treatment). The inner MSE dropped to ~11 — matching the isolated test
baseline of ~9, meaning the only remaining difference is pure renderer
divergence (ffmpeg scale + mjpeg vs mpv GL shaders) with no positional or
geometric offset.

To reproduce the measurement on any video:

```bash
venv/bin/python scripts/diff_video_alignment.py [video.mp4]
```

Outputs:

- `/tmp/align_thumb.png` / `/tmp/align_hover.png` — isolated comparison
- `/tmp/align_prod_jpeg.png` / `/tmp/align_prod_hover.png` — production
  replica
- `/tmp/align_prod_diff.png` — `|jpeg - hover| × 8` (visible as a bright
  ring if the bug recurs)

## Supporting changes in this branch

The alignment fix itself is one file; the surrounding changes came from the
same debugging session:

- **ffmpeg → mpv for thumbnail generation** (`plugins/video_plugin.py`) —
  addresses hypothesis A. Eliminates renderer-pipeline divergence as a
  contributor to visible differences.
- **Per-plugin cache invalidation** (`plugins/base_plugin.py`,
  `core/thumbnail_manager.py`, `core/db/image_table.py`) — `BasePlugin.cache_version`
  class attribute + `plugin_cache_versions.json` marker. Bumping
  `VideoPlugin.cache_version` invalidates only video thumbnails, not the
  entire cache. See `docs/video-thumbnails.md`.
- **`keepaspect="yes"`** (`gui/video_view.py`) — inert for square thumbs but
  the more principled choice for any future non-square widget paths.
- **Standalone diff tool** (`scripts/diff_video_alignment.py`) — 270 LOC. Run
  it on any video to get actual pixel-level data; no more guessing.

## Lessons

1. **Get data before iterating.** Three of our first four actions turned out
   to be wrong or inert. Each was based on a plausible story. Building the
   pixel-diff tool took ~30 minutes and immediately localised the bug to the
   outer 2 px ring.
2. **`QOpenGLWidget.grabFramebuffer()` needs a synchronous `repaint()`**, not
   an async `update()`, or the grab races the paint and returns black.
3. **`QLabel` clips pixmaps to `contentsRect`**, not to the widget's `rect()`.
   Any overlay you place on top of a QLabel must match that clipped region,
   not the label's outer rect — or scale the pixmap so there's nothing to
   clip.
4. When a visual bug has multiple plausible causes, split the problem into
   regions and measure each. "Border ring has MSE 2642, interior has 92" is
   a much stronger signal than "the overall image looks wrong."
