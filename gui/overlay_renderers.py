"""Built-in overlay renderers.

Renderer signature: ``(QPainter, QRect, dict) -> None``.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen


def _star_path(cx: float, cy: float, outer_r: float) -> QPainterPath:
    path = QPainterPath()
    inner_r = outer_r * 0.4
    for i in range(5):
        angle = math.radians(-90 + i * 72)
        ox = cx + outer_r * math.cos(angle)
        oy = cy + outer_r * math.sin(angle)
        angle2 = math.radians(-90 + i * 72 + 36)
        ix = cx + inner_r * math.cos(angle2)
        iy = cy + inner_r * math.sin(angle2)
        if i == 0:
            path.moveTo(ox, oy)
        else:
            path.lineTo(ox, oy)
        path.lineTo(ix, iy)
    path.closeSubpath()
    return path


def _dice_positions(count: int) -> list[tuple[float, float]]:
    """Return normalised (x, y) positions in [-1, 1] using dice-face layouts."""
    d = 0.55  # distance from centre to corner dots
    if count == 0:
        return []
    if count == 1:
        return [(0, 0)]
    if count == 2:
        return [(-d, -d), (d, d)]
    if count == 3:
        return [(-d, -d), (0, 0), (d, d)]
    if count == 4:
        return [(-d, -d), (d, -d), (-d, d), (d, d)]
    # 5
    return [(-d, -d), (d, -d), (0, 0), (-d, d), (d, d)]


def render_stars(painter: QPainter, rect: QRect, params: dict) -> None:
    count = params.get("count", 0)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)

    rect_w, rect_h = rect.width(), rect.height()
    cx = rect.x() + rect_w / 2
    cy = rect.y() + rect_h / 2

    if count == 0:
        star_r = min(rect_w, rect_h) * 0.12
        painter.setPen(QPen(QColor(160, 160, 160), 2.0))
        dash_w = star_r * 1.6
        painter.drawLine(
            int(cx - dash_w / 2), int(cy),
            int(cx + dash_w / 2), int(cy),
        )
    else:
        spread = min(rect_w, rect_h) * 0.28
        star_r = min(rect_w, rect_h) * 0.10
        gold = QColor(255, 200, 50)
        painter.setPen(Qt.NoPen)
        painter.setBrush(gold)
        for dx, dy in _dice_positions(count):
            sx = cx + dx * spread
            sy = cy + dy * spread
            path = _star_path(sx, sy, star_r)
            painter.drawPath(path)

    painter.restore()


def render_badge(painter: QPainter, rect: QRect, params: dict) -> None:
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)

    color = QColor(params.get("color", "#ff0000"))
    text = params.get("text", "")

    size = int(min(rect.width(), rect.height()) * 0.25)
    size = max(size, 14)

    bx = rect.x() + (rect.width() - size) // 2
    by = rect.y() + (rect.height() - size) // 2
    badge_rect = QRect(bx, by, size, size)

    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(badge_rect)

    if text:
        painter.setPen(QColor(255, 255, 255))
        font = painter.font()
        font.setPixelSize(max(size // 2, 8))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(badge_rect, Qt.AlignCenter, text[:3])

    painter.restore()
