from __future__ import annotations
import time
import logging
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPointF, QRect
from PySide6.QtGui import QPixmap, QColor, QPainter

from gui.components.item_card import ItemCard
from core.event_system import event_system, EventType, InspectorEventData
from gui.overlay_manager import OverlayManager
from gui.overlay_renderers import render_stars
from core.folder_node import FolderNode

logger = logging.getLogger(__name__)


class ThumbnailLabel(ItemCard):

    def __init__(self, file_path: str, size: int, config: dict):
        super().__init__(size, config)
        self.file_path = file_path
        self.original_path = file_path
        self.size = size
        self.loaded = False
        self.config = config

        self._original_idx: int = -1
        self._overlay_manager: OverlayManager | None = None
        self._display_rating: Optional[int] = None  # set by ThumbnailViewWidget in ratings mode

        # Folder card state
        self.is_folder: bool = False
        self._folder_node: FolderNode | None = None
        self._folder_preview_pixmaps: list | None = None  # cached scaled QPixmaps

        # Throttle inspector events to ~60 fps so rapid mouse movement does not
        # flood the event system and block the GUI thread with socket calls.
        self._pending_norm_pos: Optional[QPointF] = None
        self._inspector_timer = QTimer(self)
        self._inspector_timer.setSingleShot(True)
        self._inspector_timer.setInterval(16)  # ~60 fps
        self._inspector_timer.timeout.connect(self._flushInspectorEvent)

    def updateThumbnail(self, pixmap: QPixmap):
        if not pixmap.isNull():
            # Don't upscale: only scale down if the pixmap exceeds the label size.
            if pixmap.width() > self.size or pixmap.height() > self.size:
                scaled = pixmap.scaled(
                    self.size,
                    self.size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation)
            else:
                scaled = pixmap
            self.setPixmap(scaled)
            self.loaded = True

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.ignore()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, 'is_folder', False):
            self._queueFolderInspectorEvent(event.position())
        else:
            self._queueInspectorEvent(event.position())
        super().mouseMoveEvent(event)

    def _queueInspectorEvent(self, pos: QPointF):
        try:
            widget_rect = self.rect()
            if widget_rect.width() > 0 and widget_rect.height() > 0:
                norm_x = max(0.0, min(1.0, pos.x() / widget_rect.width()))
                # Invert Y: Qt has (0,0) at top-left, we want (0,0) at bottom-left
                norm_y = max(0.0, min(1.0, 1.0 - (pos.y() / widget_rect.height())))
                self._pending_norm_pos = QPointF(norm_x, norm_y)
                if not self._inspector_timer.isActive():
                    self._inspector_timer.start()
        except (AttributeError, TypeError) as e:
            # why: rect() can return garbage dimensions during widget teardown if a
            # mouse event fires after hide() but before deletion.
            logger.error("Error queuing inspector event from thumbnail: %s", e, exc_info=True)

    def _flushInspectorEvent(self):
        # Folder cards use a separate image path from the folder's contents
        folder_image = getattr(self, '_pending_folder_image', None)
        if folder_image:
            self._pending_folder_image = None
            self._pending_norm_pos = None
            event_system.publish(InspectorEventData(
                event_type=EventType.INSPECTOR_UPDATE,
                source="thumbnail_view",
                timestamp=time.time(),
                image_path=folder_image,
                normalized_position=QPointF(0.5, 0.5),
                cache_only=False,
            ))
            return
        pos = self._pending_norm_pos
        if pos is None:
            return
        self._pending_norm_pos = None
        event_system.publish(InspectorEventData(
            event_type=EventType.INSPECTOR_UPDATE,
            source="thumbnail_view",
            timestamp=time.time(),
            image_path=self.original_path,
            normalized_position=pos,
        ))

    def _queueFolderInspectorEvent(self, pos: QPointF):
        node = getattr(self, '_folder_node', None)
        if not node or not node.image_paths:
            return
        try:
            widget_rect = self.rect()
            if widget_rect.width() <= 0:
                return
            norm_x = max(0.0, min(1.0, pos.x() / widget_rect.width()))
            idx = min(int(norm_x * len(node.image_paths)), len(node.image_paths) - 1)
            image_path = node.image_paths[idx]
            # Publish with centered position so the inspector shows the full image
            self._pending_norm_pos = QPointF(0.5, 0.5)
            self._pending_folder_image = image_path
            if not self._inspector_timer.isActive():
                self._inspector_timer.start()
        except (AttributeError, TypeError) as e:
            logger.error("Error queuing folder inspector event: %s", e, exc_info=True)

    def paintEvent(self, event):
        if getattr(self, 'is_folder', False):
            self._paint_folder_card(event)
            return
        super().paintEvent(event)
        has_overlay = self._overlay_manager and self._overlay_manager.has_overlays(self._original_idx)
        show_rating = self._display_rating is not None and self._display_rating > 0
        if has_overlay or show_rating:
            try:
                painter = QPainter(self)
                if has_overlay:
                    self._overlay_manager.paint(painter, self.rect(), self._original_idx)
                if show_rating:
                    r = self.rect()
                    strip_h = max(r.height() // 3, 24)
                    rating_rect = QRect(r.x(), r.y() + r.height() - strip_h, r.width(), strip_h)
                    render_stars(painter, rating_rect, {"count": self._display_rating})
                painter.end()
            except Exception:
                # why: renderer crash must not leave QPainter open or break label rendering
                logger.exception("[overlay] paintEvent error for idx %d", self._original_idx)

    # Class-level constants for folder card rendering (avoid per-paint allocation)
    _FOLDER_ICON_FONT = None
    _FOLDER_BADGE_FONT = None
    _FOLDER_BADGE_FM = None
    _FOLDER_ICON_PEN = None
    _FOLDER_BADGE_BG = None
    _FOLDER_BADGE_FG = None

    @classmethod
    def _ensure_folder_fonts(cls):
        if cls._FOLDER_ICON_FONT is None:
            from PySide6.QtGui import QFont, QPen, QBrush, QFontMetrics
            cls._FOLDER_ICON_FONT = QFont("sans-serif", 14)
            cls._FOLDER_BADGE_FONT = QFont("sans-serif", 9, QFont.Bold)
            cls._FOLDER_BADGE_FM = QFontMetrics(cls._FOLDER_BADGE_FONT)
            cls._FOLDER_ICON_PEN = QPen(QColor(200, 200, 200, 200))
            cls._FOLDER_BADGE_BG = QBrush(QColor(0, 0, 0, 160))
            cls._FOLDER_BADGE_FG = QPen(QColor(255, 255, 255))

    def _paint_folder_card(self, event):
        from PySide6.QtCore import QRectF

        self._ensure_folder_fonts()
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        node = self._folder_node

        # Draw 2x2 preview mosaic from cached pixmaps
        if self._folder_preview_pixmaps:
            inset = 6
            gap = 2
            available = rect.width() - 2 * inset
            cell = (available - gap) // 2
            for i, scaled in enumerate(self._folder_preview_pixmaps):
                row, col = divmod(i, 2)
                cx = inset + col * (cell + gap)
                cy = inset + row * (cell + gap)
                dx = cx + (cell - scaled.width()) // 2
                dy = cy + (cell - scaled.height()) // 2
                painter.drawPixmap(dx, dy, scaled)

        # Folder icon (bottom-left corner)
        painter.setFont(self._FOLDER_ICON_FONT)
        painter.setPen(self._FOLDER_ICON_PEN)
        painter.drawText(6, rect.height() - 6, "\U0001F4C1")

        # Count badge (top-right corner)
        if node:
            count = node.recursive_count or node.image_count
            if count > 0:
                badge_text = str(count)
                painter.setFont(self._FOLDER_BADGE_FONT)
                fm = self._FOLDER_BADGE_FM
                tw = fm.horizontalAdvance(badge_text) + 8
                th = fm.height() + 4
                badge_rect = QRectF(rect.width() - tw - 4, 4, tw, th)
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._FOLDER_BADGE_BG)
                painter.drawRoundedRect(badge_rect, 4, 4)
                painter.setPen(self._FOLDER_BADGE_FG)
                painter.drawText(badge_rect, Qt.AlignCenter, badge_text)

        # Star rating (bottom strip, same as image rating display)
        node_rating = node.rating if node else 0
        if node_rating > 0:
            strip_h = max(rect.height() // 3, 24)
            rating_rect = QRect(rect.x(), rect.y() + rect.height() - strip_h, rect.width(), strip_h)
            render_stars(painter, rating_rect, {"count": node_rating})

        painter.end()

    def _cache_folder_previews(self):
        node = self._folder_node
        if not node or not node.preview_paths:
            self._folder_preview_pixmaps = None
            return
        rect = self.rect() if self.rect().width() > 0 else None
        if not rect:
            self._folder_preview_pixmaps = None
            return
        inset = 6
        gap = 2
        cell = (rect.width() - 2 * inset - gap) // 2
        pixmaps = []
        for thumb_path in node.preview_paths[:4]:
            pix = QPixmap(thumb_path)
            if not pix.isNull():
                pixmaps.append(pix.scaled(cell, cell, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._folder_preview_pixmaps = pixmaps if pixmaps else None

