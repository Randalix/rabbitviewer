"""Date range filter dialog.

On open:
  - Full track range = all dates in the current folder.
  - Handles pre-positioned to the date span of currently visible images.
Dragging narrows or broadens the filter; the filter is applied live (200 ms
debounce handled by FilterController).
Images without a usable date (date_taken and mtime both absent) are excluded
when the filter is active.
"""

from __future__ import annotations

import time
import threading
import logging
from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from core.event_system import event_system, EventType, DateFilterEventData
from gui.components.date_range_slider import DateRangeSlider

logger = logging.getLogger(__name__)



def _date_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %d, %Y  %H:%M")


class DateFilterDialog(QDialog):
    _data_ready = Signal(object, object)  # (full_range, visible_range)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by Date")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(480)

        self._service = None
        self._all_paths: list = []
        self._visible_paths: list = []
        self._active: bool = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self._range_label = QLabel("Loading…")
        self._range_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._range_label)

        self._slider = DateRangeSlider(self)
        self._slider.range_changed.connect(self._on_slider_changed)
        layout.addWidget(self._slider)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self._clear_btn = QPushButton("Clear filter")
        self._clear_btn.clicked.connect(self._clear)
        bottom.addWidget(self._clear_btn)
        layout.addLayout(bottom)

        self.adjustSize()

        QShortcut(QKeySequence("Esc"), self).activated.connect(self._on_esc)

        self._data_ready.connect(self._on_data_ready)

    # ------------------------------------------------------------------
    # Public

    def open_for(self, service, all_paths: list, visible_paths: list) -> None:
        """Populate and show. Call from the main thread."""
        self._service = service
        self._all_paths = list(all_paths)
        self._visible_paths = list(visible_paths)
        self._range_label.setText("Loading date range…")
        self.show()
        self.raise_()
        self.activateWindow()
        threading.Thread(target=self._fetch_ranges, daemon=True).start()

    def clear_filter(self) -> None:
        self._clear()

    # ------------------------------------------------------------------
    # Background data fetch

    def _fetch_ranges(self) -> None:
        if not self._service:
            return
        try:
            full = self._service.get_date_range_for_paths(self._all_paths)
            visible = self._service.get_date_range_for_paths(self._visible_paths)
            self._data_ready.emit(full, visible)
        except Exception:
            logger.exception("Failed to fetch date ranges")
            self._data_ready.emit(None, None)

    def _on_data_ready(self, full_range, visible_range) -> None:
        if full_range is None:
            self._range_label.setText("No date information available.")
            return

        t_min, t_max = full_range
        self._slider.set_full_range(t_min, t_max)

        if visible_range:
            lo, hi = visible_range
        else:
            lo, hi = t_min, t_max

        self._slider.set_handles(lo, hi)

        self._range_label.setText(
            f"Full range:  {_date_str(t_min)}  –  {_date_str(t_max)}"
        )

        self._active = True
        self._emit_filter()

    # ------------------------------------------------------------------
    # Slider interaction

    def _on_slider_changed(self, lo: float, hi: float) -> None:
        self._active = True
        self._emit_filter()

    def _emit_filter(self) -> None:
        if not self._active:
            return
        event_system.publish(DateFilterEventData(
            event_type=EventType.DATE_FILTER_CHANGED,
            source="date_filter_dialog",
            timestamp=time.time(),
            date_range=(self._slider.lo, self._slider.hi),
        ))

    # ------------------------------------------------------------------
    # Clear / close

    def _clear(self) -> None:
        self._active = False
        event_system.publish(DateFilterEventData(
            event_type=EventType.DATE_FILTER_CHANGED,
            source="date_filter_dialog",
            timestamp=time.time(),
            date_range=None,
        ))
        self.hide()

    def _on_esc(self) -> None:
        self._clear()
