"""Two-handle range slider with a non-linear time axis.

The slider position maps to timestamps via a power-law curve whose exponent
adapts to the overall span: short spans (hours) are nearly linear; long spans
(years) are strongly curved so each pixel covers more time near the extremes.
No snapping — the mapping is continuous and smooth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget, QSizePolicy


_HANDLE_RADIUS = 8
_TRACK_HEIGHT = 4
_PADDING = 20        # horizontal padding so handles don't clip


def _alpha_for_span(span: float) -> float:
    """Power-law exponent based on total time span in seconds."""
    if span < 3_600:           # < 1 hour — nearly linear
        return 1.0
    if span < 86_400:          # < 1 day
        return 1.5
    if span < 86_400 * 30:    # < 1 month
        return 2.0
    if span < 86_400 * 365:   # < 1 year
        return 2.5
    return 3.0


def _fmt_ts(ts: float, span: float) -> str:
    """Human-readable label for a timestamp given the visible span."""
    dt = datetime.fromtimestamp(ts)
    if span < 3_600:
        return dt.strftime("%H:%M:%S")
    if span < 86_400:
        return dt.strftime("%H:%M")
    if span < 86_400 * 365:
        return dt.strftime("%b %d")
    return dt.strftime("%b %d %Y")


class DateRangeSlider(QWidget):
    """Emits range_changed(min_ts, max_ts) as the user drags the handles."""

    range_changed = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t_min: float = 0.0
        self._t_max: float = 1.0
        self._lo: float = 0.0
        self._hi: float = 1.0
        self._dragging: Optional[str] = None  # "lo" | "hi"
        self._drag_offset: float = 0.0

        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API

    def set_full_range(self, t_min: float, t_max: float) -> None:
        """Set the absolute min/max of the slider track (full folder range)."""
        self._t_min = t_min
        self._t_max = max(t_max, t_min + 1)  # guard zero-span
        self._lo = t_min
        self._hi = self._t_max
        self.update()

    def set_handles(self, lo: float, hi: float) -> None:
        """Position the two handles (clamped to full range)."""
        self._lo = max(self._t_min, min(lo, self._t_max))
        self._hi = max(self._t_min, min(hi, self._t_max))
        if self._lo > self._hi:
            self._lo, self._hi = self._hi, self._lo
        self.update()

    @property
    def lo(self) -> float:
        return self._lo

    @property
    def hi(self) -> float:
        return self._hi

    # ------------------------------------------------------------------
    # Non-linear mapping

    def _alpha(self) -> float:
        return _alpha_for_span(self._t_max - self._t_min)

    def _pos_to_ts(self, px: float) -> float:
        """Map pixel x → timestamp (non-linear)."""
        track_w = self._track_width()
        if track_w <= 0:
            return self._t_min
        p = max(0.0, min(1.0, (px - _PADDING) / track_w))
        frac = p ** self._alpha()
        return self._t_min + frac * (self._t_max - self._t_min)

    def _ts_to_pos(self, ts: float) -> float:
        """Map timestamp → pixel x (non-linear)."""
        span = self._t_max - self._t_min
        if span <= 0:
            return float(_PADDING)
        frac = (ts - self._t_min) / span
        frac = max(0.0, min(1.0, frac))
        alpha = self._alpha()
        p = frac ** (1.0 / alpha) if alpha > 0 else frac
        return _PADDING + p * self._track_width()

    def _track_width(self) -> float:
        return max(1.0, self.width() - 2 * _PADDING)

    # ------------------------------------------------------------------
    # Paint

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        track_y = self.height() - _PADDING
        lo_x = int(self._ts_to_pos(self._lo))
        hi_x = int(self._ts_to_pos(self._hi))

        # Track: full
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#555555"))
        p.drawRoundedRect(
            QRect(_PADDING, track_y - _TRACK_HEIGHT // 2,
                  int(self._track_width()), _TRACK_HEIGHT),
            2, 2,
        )

        # Track: selected range
        p.setBrush(QColor("#4a90d9"))
        p.drawRoundedRect(
            QRect(lo_x, track_y - _TRACK_HEIGHT // 2,
                  hi_x - lo_x, _TRACK_HEIGHT),
            2, 2,
        )

        # Handles
        for x, active in [(lo_x, self._dragging == "lo"),
                           (hi_x, self._dragging == "hi")]:
            p.setBrush(QColor("#ffffff") if active else QColor("#cccccc"))
            p.setPen(QPen(QColor("#4a90d9"), 2))
            p.drawEllipse(
                QPoint(x, track_y),
                _HANDLE_RADIUS, _HANDLE_RADIUS,
            )

        # Date labels above handles
        span = self._t_max - self._t_min
        font = self.font()
        font.setPointSize(9)
        p.setFont(font)
        p.setPen(QColor("#dddddd"))
        fm = QFontMetrics(font)

        lo_label = _fmt_ts(self._lo, span)
        hi_label = _fmt_ts(self._hi, span)
        lo_lw = fm.horizontalAdvance(lo_label)
        hi_lw = fm.horizontalAdvance(hi_label)

        lo_lx = max(_PADDING, min(lo_x - lo_lw // 2, self.width() - _PADDING - lo_lw))
        hi_lx = max(_PADDING, min(hi_x - hi_lw // 2, self.width() - _PADDING - hi_lw))

        label_y = track_y - _HANDLE_RADIUS - 4
        p.drawText(lo_lx, label_y, lo_label)
        p.drawText(hi_lx, label_y, hi_label)

    # ------------------------------------------------------------------
    # Mouse

    def _hit(self, x: float, ts: float) -> bool:
        return abs(x - self._ts_to_pos(ts)) <= _HANDLE_RADIUS + 4

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        x = event.position().x()
        lo_x = self._ts_to_pos(self._lo)
        hi_x = self._ts_to_pos(self._hi)
        # Pick the closer handle when overlapping
        if self._hit(x, self._lo) and self._hit(x, self._hi):
            self._dragging = "lo" if abs(x - lo_x) <= abs(x - hi_x) else "hi"
        elif self._hit(x, self._lo):
            self._dragging = "lo"
        elif self._hit(x, self._hi):
            self._dragging = "hi"

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        ts = self._pos_to_ts(event.position().x())
        if self._dragging == "lo":
            self._lo = max(self._t_min, min(ts, self._hi))
        else:
            self._hi = max(self._lo, min(ts, self._t_max))
        self.update()
        self.range_changed.emit(self._lo, self._hi)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = None
