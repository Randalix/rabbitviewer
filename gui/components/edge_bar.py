from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QMouseEvent, QRegion, QTransform
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from PySide6.QtWidgets import QScrollArea
    from gui.components.virtual_grid_manager import VirtualGridManager
    from gui.thumbnail_model import ThumbnailModel

SIDE_FRACTION = 0.1  # side extensions as fraction of parent height


@dataclass
class _IndicatorEntry:
    color: QColor
    h_ratio: float = 0.0  # horizontal: fills strip from center
    v_ratio: float = 0.0  # vertical: reveals side extensions
    click_callback: Optional[Callable] = None


def _build_top_shape(w: float, bh: float, side_h: float) -> QPainterPath:
    """Build ∩ shape in top-orientation: y=0 is outer edge, open at bottom.

    Full bh thickness across the top strip and down the sides.
    Tips taper to zero at the open ends.  The inner contour flows
    smoothly with no sharp corners.
    """
    total_h = bh + side_h
    cr = bh * 1.5  # inner corner rounding spread
    flare = bh * 0.4
    cx = w / 2.0

    p = QPainterPath()
    p.moveTo(0.0, total_h)

    # Outer boundary — straight lines
    p.lineTo(0.0, 0.0)
    p.lineTo(w, 0.0)
    p.lineTo(w, total_h)

    # --- Inner contour: one smooth line ---
    # Right side taper — bows outward for most of the length
    p.cubicTo(w + flare, total_h - side_h * 0.85,
              w + flare * 0.5, total_h - side_h * 0.5,
              w - bh, bh + cr)
    # Right inner corner — flows into top strip
    p.cubicTo(w - bh, bh + cr * 0.3,
              w - bh - cr * 0.3, bh,
              w - bh - cr, bh)
    # Top strip inner edge — tapers to zero at the outer ends (x=w, x=0)
    # and peaks at full bh thickness in the center.
    p.cubicTo(w * 0.65, 0.0, cx + bh, bh * 1.2, cx, bh * 1.2)
    p.cubicTo(cx - bh, bh * 1.2, w * 0.35, 0.0, bh + cr, bh)
    # Left inner corner
    p.cubicTo(bh + cr * 0.3, bh,
              bh, bh + cr * 0.3,
              bh, bh + cr)
    # Left side taper — bows outward
    p.cubicTo(-flare * 0.5, total_h - side_h * 0.5,
              -flare, total_h - side_h * 0.85,
              0.0, total_h)

    p.closeSubpath()
    return p


class EdgeBar(QWidget):
    """Overlay at a viewport edge. Hosts named indicators that control
    a two-phase reveal (horizontal strip, then vertical sides) of a
    ∩ / ∪ shaped frame."""

    def __init__(self, parent: QWidget, edge: str, height: int = 6) -> None:
        super().__init__(parent)
        self._edge = edge
        self._bar_height = height
        self._indicators: Dict[str, _IndicatorEntry] = {}
        self._order: List[str] = []
        self._cached_shape: Optional[QPainterPath] = None
        self._cached_shape_key: tuple = ()
        self._last_clip: Optional[QRectF] = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()

    # -- public API -----------------------------------------------------------

    def add_indicator(self, name: str, color: QColor,
                      click_callback: Optional[Callable] = None) -> None:
        if name not in self._indicators:
            self._indicators[name] = _IndicatorEntry(
                color=QColor(color), click_callback=click_callback)
            self._order.append(name)

    def set_ratio(self, name: str, h_ratio: float, v_ratio: float = 0.0) -> None:
        entry = self._indicators.get(name)
        if entry is None:
            return
        h = max(0.0, min(1.0, h_ratio))
        v = max(0.0, min(1.0, v_ratio))
        if entry.h_ratio == h and entry.v_ratio == v:
            return
        entry.h_ratio = h
        entry.v_ratio = v
        self._sync_visibility()

    def remove_indicator(self, name: str) -> None:
        if name in self._indicators:
            del self._indicators[name]
            self._order.remove(name)
            self._sync_visibility()

    def reposition(self) -> None:
        p = self.parentWidget()
        if p is None:
            return
        w, ph = p.width(), p.height()
        total_h = self._bar_height + int(ph * SIDE_FRACTION)
        if self._edge == "top":
            self.setGeometry(0, 0, w, total_h)
        else:
            self.setGeometry(0, ph - total_h, w, total_h)
        self._cached_shape = None
        shape = _build_top_shape(float(w), float(self._bar_height), float(ph) * SIDE_FRACTION)
        if self._edge != "top":
            shape = QTransform.fromScale(1, -1).map(shape)
            shape.translate(0, float(total_h))
        mask = QRegion(shape.toFillPolygon(QTransform()).toPolygon())
        self.setMask(mask.intersected(QRegion(self.rect())))
        self.raise_()

    def schedule_repaint(self) -> None:
        """Batched repaint — call after all set_ratio calls are done."""
        if self.isVisible():
            self.update()

    # -- painting -------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        w = float(self.width())
        if w <= 0:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        bh = float(self._bar_height)
        side_h = float(parent.height()) * SIDE_FRACTION
        total_h = bh + side_h
        is_top = self._edge == "top"

        key = (w, bh, side_h)
        if self._cached_shape is None or self._cached_shape_key != key:
            shape = _build_top_shape(w, bh, side_h)
            if not is_top:
                shape = QTransform.fromScale(1, -1).map(shape)
                shape.translate(0, total_h)
            self._cached_shape = shape
            self._cached_shape_key = key
        shape = self._cached_shape

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for name in self._order:
            entry = self._indicators[name]
            if entry.h_ratio <= 0:
                continue
            clip_w = entry.h_ratio * w
            clip_x = (w - clip_w) / 2.0
            clip_h = bh + entry.v_ratio * side_h
            if is_top:
                clip_rect = QRectF(clip_x, 0.0, clip_w, clip_h)
            else:
                clip_rect = QRectF(clip_x, total_h - clip_h, clip_w, clip_h)
            painter.save()
            painter.setClipRect(clip_rect)
            painter.setPen(Qt.NoPen)
            painter.setBrush(entry.color)
            painter.drawPath(shape)
            painter.restore()
        painter.end()

    # -- click dispatch -------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        for name in reversed(self._order):
            entry = self._indicators[name]
            if entry.h_ratio > 0 and entry.click_callback is not None:
                entry.click_callback()
                return

    # -- internal -------------------------------------------------------------

    def _sync_visibility(self) -> None:
        visible = any(e.h_ratio > 0 for e in self._indicators.values())
        if visible and not self.isVisible():
            self.setVisible(True)
            self.reposition()
            self.setCursor(Qt.PointingHandCursor)
        elif not visible and self.isVisible():
            self.setVisible(False)
            self.setCursor(Qt.ArrowCursor)


class SelectionEdgeIndicator:
    """Drives two EdgeBars to indicate off-screen selection position."""

    NAME = "selection"

    def __init__(self, top_bar: EdgeBar, bottom_bar: EdgeBar,
                 model: ThumbnailModel, grid: VirtualGridManager,
                 scroll_area: QScrollArea, selection_source) -> None:
        self._top = top_bar
        self._bottom = bottom_bar
        self._model = model
        self._grid = grid
        self._scroll_area = scroll_area
        self._selection = selection_source
        # Cached extremes — recomputed only on selection change
        self._topmost_y: Optional[float] = None
        self._bottommost_y: Optional[float] = None

    def on_selection_changed(self) -> None:
        """Recompute cached Y extremes, then update ratios."""
        self._recompute_extremes()
        self._apply()

    def update(self) -> None:
        """Recompute ratios from cached extremes (cheap — no selection iteration)."""
        self._apply()

    def _recompute_extremes(self) -> None:
        sel = self._selection.current_selection
        topmost = bottommost = None
        thumb_size = self._grid._thumb_size
        model, grid = self._model, self._grid
        for path in sel:
            orig = model.path_to_idx.get(path)
            if orig is None:
                continue
            vis = model.original_to_visible.get(orig)
            if vis is None:
                continue
            y = grid._pos_y(vis)
            if topmost is None or y < topmost:
                topmost = y
            yb = y + thumb_size
            if bottommost is None or yb > bottommost:
                bottommost = yb
        self._topmost_y = topmost
        self._bottommost_y = bottommost

    def _apply(self) -> None:
        top_y, bot_y = self._topmost_y, self._bottommost_y
        if top_y is None or bot_y is None:
            self._clear()
            return

        scroll_y, vp_h, vp_top, vp_bot = self._viewport_info()
        half = vp_h / 2
        if half <= 0:
            self._clear()
            return

        center = scroll_y + half
        side_range = vp_h * SIDE_FRACTION

        top_h = max(0.0, min(1.0, (center - top_y) / half))
        bot_h = max(0.0, min(1.0, (bot_y - center) / half))
        top_v = max(0.0, min(1.0, (vp_top - top_y) / side_range)) if top_y < vp_top else 0.0
        bot_v = max(0.0, min(1.0, (bot_y - vp_bot) / side_range)) if bot_y > vp_bot else 0.0

        # Batch: set both bars, then repaint once each
        self._top.set_ratio(self.NAME, top_h, top_v)
        self._bottom.set_ratio(self.NAME, bot_h, bot_v)
        self._top.schedule_repaint()
        self._bottom.schedule_repaint()

    def scroll_to_nearest(self, direction: str) -> None:
        sel = self._selection.current_selection
        if not sel:
            return
        model, grid = self._model, self._grid
        _, _, vp_top, vp_bot = self._viewport_info()

        candidates = []
        for path in sel:
            orig = model.path_to_idx.get(path)
            if orig is None:
                continue
            vis = model.original_to_visible.get(orig)
            if vis is None:
                continue
            y = grid._pos_y(vis)
            if direction == "up" and y < vp_top:
                candidates.append(vis)
            elif direction == "down" and y + grid._thumb_size > vp_bot:
                candidates.append(vis)

        if not candidates:
            return
        if direction == "up":
            target = max(candidates, key=lambda vi: grid._pos_y(vi))
        else:
            target = min(candidates, key=lambda vi: grid._pos_y(vi))
        grid.ensure_visible(target, center=True)

    def _clear(self) -> None:
        self._top.set_ratio(self.NAME, 0, 0)
        self._bottom.set_ratio(self.NAME, 0, 0)

    def _viewport_info(self):
        sa = self._scroll_area
        scroll_y = sa.verticalScrollBar().value()
        vp_h = sa.viewport().height()
        return scroll_y, vp_h, scroll_y, scroll_y + vp_h
