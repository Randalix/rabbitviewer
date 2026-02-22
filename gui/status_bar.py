from PySide6.QtWidgets import QStatusBar, QLabel, QWidget, QHBoxLayout
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from typing import Optional
import logging

from gui.components.scrolling_label import ScrollingLabel

# Sentinel: rating section is blank because no image is hovered.
# Distinct from None (image hovered, but no rating metadata available → "—").
_CLEARED = object()


class CustomStatusBar(QStatusBar):
    """3-section status bar: filepath (left), rating (centre), process (right)."""

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager

        # Raw stored values for each section.
        # _raw_rating is one of: _CLEARED (blank), None (em-dash), or int (stars).
        self._raw_filepath: str = ""
        self._raw_rating: object = _CLEARED
        self._raw_process: str = ""

        self._process_timer = QTimer(self)
        self._process_timer.setSingleShot(True)
        self._process_timer.timeout.connect(self._clear_process)

        self._build_layout()
        self._apply_font_settings()

    def _build_layout(self):
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        self._filepath_label = ScrollingLabel()
        layout.addWidget(self._filepath_label, 3)

        self._rating_label = QLabel()
        self._rating_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self._rating_label.setFixedWidth(110)
        self._rating_label.setTextFormat(Qt.RichText)
        self._rating_label.setStyleSheet("padding-bottom: 3px;")
        layout.addWidget(self._rating_label)

        self._process_label = QLabel()
        self._process_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._process_label, 2)

        self.addWidget(container, 1)

    def _apply_font_settings(self):
        try:
            if self.config_manager:
                font_family = self.config_manager.get("gui.statusbar_font", "Arial")
                font_size = self.config_manager.get("gui.statusbar_font_size", 10)
            else:
                font_family = "Arial"
                font_size = 10
            font = QFont(font_family, font_size)
            for label in (self._filepath_label, self._process_label):
                label.setFont(font)
            rating_font = QFont(font_family, font_size + 4)
            self._rating_label.setFont(rating_font)
        except Exception as e:  # why: config_manager is user-supplied; malformed config must not crash the status bar at startup
            logging.warning(f"Could not apply status bar font settings: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setFilepath(self, path: str):
        self._raw_filepath = path
        self._refresh_elision()

    def setRating(self, rating: Optional[int]):
        """Show rating for the currently hovered/loaded image. None → em-dash (no metadata)."""
        self._raw_rating = rating
        self._refresh_elision()

    def clearRating(self):
        self._raw_rating = _CLEARED
        self._rating_label.setText("")

    def setProcessMessage(self, message: str, timeout: int = 0):
        self._process_timer.stop()
        self._raw_process = message
        self._refresh_elision()
        if timeout > 0:
            self._process_timer.start(timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear_process(self):
        self._raw_process = ""
        self._refresh_elision()

    @staticmethod
    def _rating_text(rating: Optional[int]) -> str:
        if rating is None:
            return "\u2014"  # em-dash: image hovered but no rating metadata
        filled = min(max(rating, 0), 5)
        empty = 5 - filled
        parts = []
        if filled:
            parts.append(f'<span style="color:#F5A623;">{"\u2605" * filled}</span>')
        if empty:
            parts.append(f'<span style="color:#555555;">{"\u2606" * empty}</span>')
        return "".join(parts) if parts else ""

    def _refresh_elision(self):
        self._filepath_label.setText(self._raw_filepath)
        self._filepath_label.setToolTip(self._raw_filepath)

        if self._raw_rating is not _CLEARED:
            self._rating_label.setText(self._rating_text(self._raw_rating))  # type: ignore[arg-type]
        # why: clearRating set "" directly; refreshing here would re-render an unwanted em-dash

        fm_pr = QFontMetrics(self._process_label.font())
        available_pr = self._process_label.width()
        if available_pr > 0:
            elided_pr = fm_pr.elidedText(self._raw_process, Qt.ElideRight, available_pr)
        else:
            elided_pr = self._raw_process
        self._process_label.setText(elided_pr)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_elision()
