"""Two-handle range slider with a non-linear time axis.

Uses an exponential curve  frac = (b^p - 1) / (b - 1)  to map slider
position p ∈ [0, 1] to a time fraction.  Unlike a power-law, this curve
has a non-zero slope at p=0 so the slider is always responsive; the ratio
of coarseness between the right and left ends is simply b.  b adapts to
the total span so short ranges behave nearly linearly while year-scale
ranges give hours of precision at the left and days at the right.

Labels above the handles are formatted from the handle span (not the full
range) so they always show enough precision to distinguish the positions.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget, QSizePolicy


_HANDLE_RADIUS = 8
_TRACK_HEIGHT = 4
_PADDING = 20  # horizontal padding so handles don't clip the widget edge


def _base_for_span(span: float) -> float:
    """Exponential base b that controls coarseness ratio right/left."""
    if span < 3_600:           # < 1 hour — mild curve
        return 2.0
    if span < 86_400:          # < 1 day
        return 8.0
    if span < 86_400 * 30:    # < 1 month
        return 30.0
    if span < 86_400 * 365:   # < 1 year
        return 100.0
    return 300.0


def _fmt_ts(ts: float, handle_span: float) -> str:
    """Format timestamp label at a precision appropriate for the handle span."""
    dt = datetime.fromtimestamp(ts)
    if handle_span < 3_600:          # handles < 1 h apart → show seconds
        return dt.strftime("%H:%M:%S")
    if handle_span < 86_400:         # handles < 1 day apart → show H:M
        return dt.strftime("%b %d  %H:%M")
    if handle_span < 86_400 * 365:   # handles < 1 year apart → show date
        return dt.strftime("%b %d, %Y")
    return dt.strftime("%b %Y")


class DateRangeSlider(QWidget):
    """Emits range_changed(lo_ts, hi_ts) continuously while dragging."""

    range_changed = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t_min: float = 0.0
        self._t_max: float = 1.0
        self._lo: float = 0.0
        self._hi: float = 1.0
        self._dragging: Optional[str] = None  # "lo" | "hi"

        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API

    def set_full_range(self, t_min: float, t_max: float) -> None:
        self._t_min = t_min
        self._t_max = max(t_max, t_min + 1)
        self._lo = t_min
        self._hi = self._t_max
        self.update()

    def set_handles(self, lo: float, hi: float) -> None:
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
    # Non-linear mapping  frac = (b^p - 1) / (b - 1)

    def _base(self) -> float:
        return _base_for_span(self._t_max - self._t_min)

    def _pos_to_ts(self, px: float) -> float:
        track_w = self._track_width()
        if track_w <= 0:
            return self._t_max
        p = max(0.0, min(1.0, (px - _PADDING) / track_w))
        b = self._base()
        frac = (b ** p - 1.0) / (b - 1.0)
        # Left (p=0) = newest (t_max), right (p=1) = oldest (t_min)
        return self._t_max - frac * (self._t_max - self._t_min)

    def _ts_to_pos(self, ts: float) -> float:
        span = self._t_max - self._t_min
        if span <= 0:
            return float(_PADDING)
        # Flip: newest dates map to low p (left), oldest to high p (right)
        frac = max(0.0, min(1.0, (self._t_max - ts) / span))
        b = self._base()
        p = math.log(1.0 + frac * (b - 1.0)) / math.log(b)
        return _PADDING + p * self._track_width()

    def _track_width(self) -> float:
        return max(1.0, self.width() - 2 * _PADDING)

    # ------------------------------------------------------------------
    # Paint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track_y = self.height() - _PADDING
        # After axis flip: hi (newer) is on the LEFT, lo (older) is on the RIGHT
        hi_x = int(self._ts_to_pos(self._hi))  # left handle
        lo_x = int(self._ts_to_pos(self._lo))  # right handle

        # Full track
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#555555"))
        painter.drawRoundedRect(
            QRect(_PADDING, track_y - _TRACK_HEIGHT // 2,
                  int(self._track_width()), _TRACK_HEIGHT),
            2, 2,
        )

        # Selected range (hi_x is left of lo_x after axis flip)
        painter.setBrush(QColor("#4a90d9"))
        painter.drawRoundedRect(
            QRect(hi_x, track_y - _TRACK_HEIGHT // 2,
                  max(0, lo_x - hi_x), _TRACK_HEIGHT),
            2, 2,
        )

        # Handles
        for x, is_active in [(hi_x, self._dragging == "hi"),
                              (lo_x, self._dragging == "lo")]:
            painter.setBrush(QColor("#ffffff") if is_active else QColor("#cccccc"))
            painter.setPen(QPen(QColor("#4a90d9"), 2))
            painter.drawEllipse(QPoint(x, track_y), _HANDLE_RADIUS, _HANDLE_RADIUS)

        # Labels above handles — format from the handle span for correct precision
        handle_span = max(1.0, self._hi - self._lo)
        font = self.font()
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor("#dddddd"))
        fm = QFontMetrics(font)

        # hi (newer) label on the left, lo (older) label on the right
        hi_label = _fmt_ts(self._hi, handle_span)
        lo_label = _fmt_ts(self._lo, handle_span)
        hi_lw = fm.horizontalAdvance(hi_label)
        lo_lw = fm.horizontalAdvance(lo_label)

        hi_lx = max(_PADDING, min(hi_x - hi_lw // 2, self.width() - _PADDING - hi_lw))
        lo_lx = max(_PADDING, min(lo_x - lo_lw // 2, self.width() - _PADDING - lo_lw))

        label_y = track_y - _HANDLE_RADIUS - 4
        painter.drawText(hi_lx, label_y, hi_label)
        painter.drawText(lo_lx, label_y, lo_label)

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
