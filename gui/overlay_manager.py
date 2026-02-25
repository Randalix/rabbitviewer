from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QPainter

BULK_THRESHOLD = 50


@dataclass
class OverlayDescriptor:
    """Treated as immutable after show(); callers must not mutate params in place."""
    overlay_id: str
    renderer_name: str
    params: dict = field(default_factory=dict)
    position: str = "center"
    duration: Optional[int] = None  # ms; None = permanent


# Renderer callable signature: (painter: QPainter, rect: QRect, params: dict) -> None
RendererFn = Callable[[QPainter, QRect, dict], None]


def _compute_sub_rect(full_rect: QRect, position: str) -> QRect:
    if position == "center":
        return full_rect

    w, h = full_rect.width(), full_rect.height()
    hw, hh = w // 2, h // 2
    x, y = full_rect.x(), full_rect.y()

    if position == "top-left":
        return QRect(x, y, hw, hh)
    if position == "top-right":
        return QRect(x + w - hw, y, hw, hh)
    if position == "bottom-left":
        return QRect(x, y + h - hh, hw, hh)
    if position == "bottom-right":
        return QRect(x + w - hw, y + h - hh, hw, hh)

    return full_rect


class OverlayManager:
    """Transient overlays auto-remove after their duration via QTimer.singleShot."""

    def __init__(self, request_update: Optional[Callable[[int], None]] = None) -> None:
        self._overlays: dict[int, dict[str, OverlayDescriptor]] = {}
        self._renderers: dict[str, RendererFn] = {}
        self._timers: dict[tuple[int, str], QTimer] = {}
        self._request_update = request_update

    def register_renderer(self, name: str, fn: RendererFn) -> None:
        self._renderers[name] = fn

    def show(self, idx: int, descriptor: OverlayDescriptor) -> None:
        logging.debug("[overlay] show idx=%d renderer=%s duration=%s",
                      idx, descriptor.renderer_name, descriptor.duration)
        bucket = self._overlays.setdefault(idx, {})

        timer_key = (idx, descriptor.overlay_id)
        old_timer = self._timers.pop(timer_key, None)
        if old_timer is not None:
            old_timer.stop()

        bucket[descriptor.overlay_id] = descriptor

        if descriptor.duration is not None:
            timer = QTimer()
            timer.setSingleShot(True)
            timer.setInterval(descriptor.duration)
            oid = descriptor.overlay_id
            timer.timeout.connect(lambda i=idx, o=oid: self._on_timer_expired(i, o))
            self._timers[timer_key] = timer
            timer.start()

    def _on_timer_expired(self, idx: int, overlay_id: str) -> None:
        self.remove(idx, overlay_id)
        if self._request_update:
            self._request_update(idx)

    def remove(self, idx: int, overlay_id: str) -> None:
        bucket = self._overlays.get(idx)
        if bucket:
            bucket.pop(overlay_id, None)
            if not bucket:
                del self._overlays[idx]

        timer_key = (idx, overlay_id)
        old_timer = self._timers.pop(timer_key, None)
        if old_timer is not None:
            old_timer.stop()

    def remove_all_for_idx(self, idx: int) -> None:
        bucket = self._overlays.pop(idx, None)
        if bucket:
            for oid in list(bucket):
                timer_key = (idx, oid)
                old_timer = self._timers.pop(timer_key, None)
                if old_timer is not None:
                    old_timer.stop()

    def has_overlays(self, idx: int) -> bool:
        return idx in self._overlays

    def paint(self, painter: QPainter, rect: QRect, idx: int) -> None:
        bucket = self._overlays.get(idx)
        if not bucket:
            return

        for descriptor in bucket.values():
            renderer = self._renderers.get(descriptor.renderer_name)
            if renderer is None:
                logging.warning("No renderer registered for %r", descriptor.renderer_name)
                continue
            sub_rect = _compute_sub_rect(rect, descriptor.position)
            renderer(painter, sub_rect, descriptor.params)
