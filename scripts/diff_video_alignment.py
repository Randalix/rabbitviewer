#!/usr/bin/env python3
"""Diff the cached JPEG thumbnail against the actual hover GL framebuffer.

Usage:
    venv/bin/python scripts/diff_video_alignment.py [video.mp4]

If no video path is supplied, a synthetic 1024x1024 marker mp4 is generated
via ffmpeg lavfi (gray background, 32x32 colored squares in each corner, a
4x4 yellow dot at center) — any pixel-level shift is trivially visible.

Runs TWO tests and reports both:

1. Isolated: 128x128 VideoView, no label/border. Measures raw pipeline divergence.
2. Production replica: ItemCard with 2px border + thumbnail pixmap + VideoView
   positioned via the same _pixmap_rect_in_label geometry the grid uses. This
   is the comparison that reflects what the user actually sees on hover.

Outputs (in /tmp):
    align_thumb.png       — cached thumbnail re-saved as png
    align_hover.png       — isolated VideoView GL framebuffer
    align_diff.png        — |thumb - hover| * 8 (isolated)
    align_prod_jpeg.png   — ItemCard showing just the thumbnail
    align_prod_hover.png  — ItemCard composited with the VideoView overlay
    align_prod_diff.png   — |jpeg - hover| * 8 (production)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Put project root on sys.path so plugins/ and gui/ import cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QElapsedTimer, QSize
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QStyle

from plugins.video_plugin import VideoPlugin
from gui.video_view import VideoView
from gui.components.item_card import ItemCard

THUMBNAIL_SIZE = 128
FIRST_FRAME_TIMEOUT_MS = 5000
MAX_SHIFT = 6  # search ±6 px — more than enough for a 1-2 px jump

# Matches the thumbnail-grid card config knobs (see gui/components/item_card.py).
CARD_CONFIG = {
    "border_width": 2,
    "select_border_color": "orange",
    "hover_border_color": "#2d59b6",
    "placeholder_color": "#1a1a1a",
}


def generate_marker_video(out: Path) -> str:
    """Make a 2s 1024x1024 gray video with 32x32 colored corner squares."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i",
            "color=c=gray:s=1024x1024:d=2:r=30,"
            "drawbox=x=0:y=0:w=32:h=32:color=red@1.0:t=fill,"
            "drawbox=x=992:y=0:w=32:h=32:color=green@1.0:t=fill,"
            "drawbox=x=0:y=992:w=32:h=32:color=blue@1.0:t=fill,"
            "drawbox=x=992:y=992:w=32:h=32:color=white@1.0:t=fill,"
            "drawbox=x=510:y=510:w=4:h=4:color=yellow@1.0:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return str(out)


def qimage_to_array(qimg: QImage) -> np.ndarray:
    qimg = qimg.convertToFormat(QImage.Format.Format_RGB888)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.constBits()
    raw = bytes(ptr)
    bpl = qimg.bytesPerLine()
    if bpl == w * 3:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
    else:
        arr = np.empty((h, w, 3), dtype=np.uint8)
        for y in range(h):
            row = raw[y * bpl : y * bpl + w * 3]
            arr[y] = np.frombuffer(row, dtype=np.uint8).reshape(w, 3)
    return arr.copy()


def qimage_to_pil(qimg: QImage) -> Image.Image:
    return Image.fromarray(qimage_to_array(qimg))


def pad_to_square(img: Image.Image, size: int, bg=(0, 0, 0)) -> Image.Image:
    if img.size == (size, size):
        return img
    padded = Image.new("RGB", (size, size), bg)
    padded.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    return padded


def pixmap_rect_in_label(label) -> tuple[int, int, int, int]:
    """Replicates gui.thumbnail_view.ThumbnailView._pixmap_rect_in_label.

    Keep logic byte-identical with the production method — this is what makes
    the replica meaningful.
    """
    pix = label.pixmap()
    if pix is None or pix.isNull():
        return (0, 0, label.width(), label.height())
    content = label.contentsRect()
    dpr = pix.devicePixelRatio() or 1.0
    pix_size = QSize(int(round(pix.width() / dpr)),
                     int(round(pix.height() / dpr)))
    aligned = QStyle.alignedRect(
        Qt.LayoutDirection.LeftToRight,
        Qt.AlignmentFlag.AlignCenter,
        pix_size,
        content,
    )
    return aligned.x(), aligned.y(), aligned.width(), aligned.height()


def wait_for_first_frame(app: QApplication, view: VideoView) -> bool:
    got = {"v": False}
    view.firstFrameReady.connect(lambda: got.__setitem__("v", True))
    timer = QElapsedTimer()
    timer.start()
    while not got["v"] and timer.elapsed() < FIRST_FRAME_TIMEOUT_MS:
        app.processEvents()
    if not got["v"]:
        return False
    # view.update() is async and the FBO grab below can race with it;
    # view.repaint() is synchronous and guarantees paintGL has run when we
    # subsequently call grabFramebuffer(). Run it a few times to flush any
    # state the first paint needs to settle.
    for _ in range(4):
        view.repaint()
        app.processEvents()
    return True


def grab_video_fbo(view: VideoView, size: tuple[int, int]) -> Image.Image:
    qimg = view.grabFramebuffer()
    if (qimg.width(), qimg.height()) != size:
        qimg = qimg.scaled(
            size[0], size[1],
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return qimage_to_pil(qimg)


# -------------------------------------------------------------------- isolated

def run_isolated(app: QApplication, video_path: str, thumb_path: str) -> dict:
    """Compare the cached thumbnail against a 128×128 bare VideoView."""
    view = VideoView(scrub=True)
    view.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
    view.show()

    view.loadVideo(video_path, start_pct=0.1)
    if not wait_for_first_frame(app, view):
        raise TimeoutError("isolated: firstFrameReady not emitted")

    hover_img = grab_video_fbo(view, (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
    view.stop()
    view.hide()
    view.deleteLater()

    thumb_img = pad_to_square(Image.open(thumb_path).convert("RGB"), THUMBNAIL_SIZE)  # disk-io: diagnostic script reads generated thumbnail

    out_thumb = Path("/tmp/align_thumb.png")
    out_hover = Path("/tmp/align_hover.png")
    out_diff = Path("/tmp/align_diff.png")
    thumb_img.save(out_thumb)
    hover_img.save(out_hover)

    a = np.array(thumb_img, dtype=np.float32)
    b = np.array(hover_img, dtype=np.float32)
    zero_mse = float(((a - b) ** 2).mean())
    dy, dx, mse = find_shift(a, b, MAX_SHIFT)
    Image.fromarray((np.abs(a - b) * 8.0).clip(0, 255).astype(np.uint8)).save(out_diff)

    return {
        "zero_mse": zero_mse, "shift": (dy, dx), "best_mse": mse,
        "thumb": out_thumb, "hover": out_hover, "diff": out_diff,
    }


# ---------------------------------------------------------------- production

def run_production(app: QApplication, video_path: str, thumb_path: str) -> dict:
    """Compare the ItemCard-rendered thumbnail against the same card composited
    with the VideoView overlay placed at `_pixmap_rect_in_label` coords.

    This is the apples-to-apples test: same widget, same border stylesheet,
    same geometry math as the real grid.
    """
    label = ItemCard(THUMBNAIL_SIZE, CARD_CONFIG)
    pix = QPixmap(thumb_path)
    label.setPixmap(pix)
    label.show()
    for _ in range(4):
        app.processEvents()

    # Snapshot 1: JPEG-only state (pixmap + border). label.grab() returns a
    # pixmap at PHYSICAL pixel dimensions (size * devicePixelRatio) on Retina
    # displays; downscale to CSS size so the (ox, oy) paste offset below is
    # in the same coordinate system.
    jpeg_qimg = label.grab().toImage()
    if jpeg_qimg.width() != THUMBNAIL_SIZE or jpeg_qimg.height() != THUMBNAIL_SIZE:
        jpeg_qimg = jpeg_qimg.scaled(
            THUMBNAIL_SIZE, THUMBNAIL_SIZE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    jpeg_pil = qimage_to_pil(jpeg_qimg)

    ox, oy, pw, ph = pixmap_rect_in_label(label)
    print(f"  [prod] pixmap_rect_in_label = (x={ox}, y={oy}, w={pw}, h={ph})")
    print(f"  [prod] pixmap natural size  = {pix.width()}x{pix.height()}")

    view = VideoView(scrub=True)
    view.setFixedSize(pw, ph)
    view.show()
    view.loadVideo(video_path, start_pct=0.1)
    if not wait_for_first_frame(app, view):
        view.deleteLater()
        label.deleteLater()
        raise TimeoutError("production: firstFrameReady not emitted")

    video_pil = grab_video_fbo(view, (pw, ph))
    view.stop()
    view.hide()
    view.deleteLater()

    # Composite: paste the GL framebuffer onto the JPEG snapshot at (ox, oy).
    # In the idle state the card's stylesheet border is transparent — no need
    # to re-overlay it. The selected/hovered coloured border is a separate
    # sibling widget in production, outside the scope of this pixel-diff.
    hover_pil = jpeg_pil.copy()
    hover_pil.paste(video_pil, (ox, oy))

    label.hide()
    label.deleteLater()

    out_jpeg = Path("/tmp/align_prod_jpeg.png")
    out_hover = Path("/tmp/align_prod_hover.png")
    out_diff = Path("/tmp/align_prod_diff.png")
    jpeg_pil.save(out_jpeg)
    hover_pil.save(out_hover)

    a = np.array(jpeg_pil, dtype=np.float32)
    b = np.array(hover_pil, dtype=np.float32)
    zero_mse = float(((a - b) ** 2).mean())
    dy, dx, mse = find_shift(a, b, MAX_SHIFT)
    Image.fromarray((np.abs(a - b) * 8.0).clip(0, 255).astype(np.uint8)).save(out_diff)

    return {
        "zero_mse": zero_mse, "shift": (dy, dx), "best_mse": mse,
        "rect": (ox, oy, pw, ph),
        "jpeg": out_jpeg, "hover": out_hover, "diff": out_diff,
    }


# ---------------------------------------------------------------------- shift

def find_shift(a: np.ndarray, b: np.ndarray, max_shift: int) -> tuple[int, int, float]:
    """Best integer (dy, dx) shift of b against a, by MSE on overlap."""
    best = (0, 0, float("inf"))
    a_g = a.mean(axis=-1) if a.ndim == 3 else a.astype(np.float32)
    b_g = b.mean(axis=-1) if b.ndim == 3 else b.astype(np.float32)
    h, w = a_g.shape
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            y0, y1 = max(0, dy), min(h, h + dy)
            x0, x1 = max(0, dx), min(w, w + dx)
            cy0, cy1 = max(0, -dy), min(h, h - dy)
            cx0, cx1 = max(0, -dx), min(w, w - dx)
            if y1 - y0 < 10 or x1 - x0 < 10:
                continue
            diff = a_g[y0:y1, x0:x1] - b_g[cy0:cy1, cx0:cx1]
            mse = float((diff * diff).mean())
            if mse < best[2]:
                best = (dy, dx, mse)
    return best


# ------------------------------------------------------------------------ main

def main() -> int:
    video = sys.argv[1] if len(sys.argv) > 1 else None
    tmp = Path(tempfile.mkdtemp(prefix="align_"))

    if video is None:
        print(f"No video given — generating marker video in {tmp}")
        video = generate_marker_video(tmp / "marker.mp4")
    print(f"Using video: {video}")

    plugin = VideoPlugin(cache_dir=str(tmp), thumbnail_size=THUMBNAIL_SIZE)
    thumb_path = plugin.process_thumbnail(video, "align_test")
    if not thumb_path or not os.path.exists(thumb_path):  # disk-io: verify plugin produced the thumbnail
        print("ERROR: plugin did not produce a thumbnail")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)

    print()
    print("=" * 72)
    print("TEST 1 — ISOLATED (128x128 bare VideoView, no border)")
    print("=" * 72)
    iso = run_isolated(app, video, thumb_path)
    print(f"  MSE at zero shift : {iso['zero_mse']:.2f}")
    print(f"  Best shift        : dy={iso['shift'][0]:+d}, dx={iso['shift'][1]:+d}"
          f"   MSE={iso['best_mse']:.2f}")
    print("  Files:")
    print(f"    {iso['thumb']}")
    print(f"    {iso['hover']}")
    print(f"    {iso['diff']}")

    print()
    print("=" * 72)
    print("TEST 2 — PRODUCTION REPLICA (ItemCard + border + _pixmap_rect_in_label)")
    print("=" * 72)
    prod = run_production(app, video, thumb_path)
    ox, oy, pw, ph = prod["rect"]
    print(f"  MSE at zero shift : {prod['zero_mse']:.2f}")
    print(f"  Best shift        : dy={prod['shift'][0]:+d}, dx={prod['shift'][1]:+d}"
          f"   MSE={prod['best_mse']:.2f}")
    print(f"  Video overlay rect: ({ox}, {oy}, {pw}x{ph})  inside 128x128 card")
    print("  Files:")
    print(f"    {prod['jpeg']}   — card showing only the thumbnail")
    print(f"    {prod['hover']}  — same card composited with the video overlay")
    print(f"    {prod['diff']}   — |jpeg - hover| * 8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
