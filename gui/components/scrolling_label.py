from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontMetrics, QPainter


class ScrollingLabel(QWidget):
    """A label that ping-pong scrolls its text when it overflows the widget width.

    Starts at the trailing end of the text (filename visible), scrolls to the
    leading end, pauses, then scrolls back. Falls back to static display when
    the text fits.
    """

    _SPEED = 1        # px per tick
    _INTERVAL = 16    # ms per tick (~60 fps)
    _PAUSE = 90       # ticks at each end (~1.5 s)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._offset = 0       # how many px the text is shifted left
        self._direction = -1   # -1 = scrolling toward leading end (offset ↓)
        self._pause_remaining = 0

        self._timer = QTimer(self)
        self._timer.setInterval(self._INTERVAL)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # Public API (mirrors QLabel)
    # ------------------------------------------------------------------

    def setText(self, text: str):
        if text == self._text:
            return
        self._text = text
        self._reset()

    def text(self) -> str:
        return self._text

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _max_offset(self) -> int:
        tw = QFontMetrics(self.font()).horizontalAdvance(self._text)
        return max(0, tw - self.width())

    def _reset(self):
        max_off = self._max_offset()
        if max_off > 0:
            self._offset = max_off   # start showing the trailing end
            self._direction = -1     # first move: toward leading end
            self._pause_remaining = self._PAUSE
            self._timer.start()
        else:
            self._offset = 0
            self._timer.stop()
        self.update()

    def _tick(self):
        if self._pause_remaining > 0:
            self._pause_remaining -= 1
            return

        max_off = self._max_offset()
        if max_off == 0:
            self._offset = 0
            self._timer.stop()
            self.update()
            return

        self._offset += self._direction * self._SPEED

        if self._offset <= 0:
            self._offset = 0
            self._direction = 1
            self._pause_remaining = self._PAUSE
        elif self._offset >= max_off:
            self._offset = max_off
            self._direction = -1
            self._pause_remaining = self._PAUSE

        self.update()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        painter.setPen(self.palette().windowText().color())

        fm = QFontMetrics(self.font())
        # why: Qt text origin is baseline, not cap-height; ascent - descent recenters within the widget rect
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        painter.drawText(-self._offset, y, self._text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reset()
