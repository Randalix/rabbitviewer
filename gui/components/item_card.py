"""Base card widget with config-driven selection/hover borders.

Subclasses override ``_style_*()`` hooks to customise appearance while
keeping border, selection, and hover behaviour identical across all
card-based views (thumbnail grid, face palette, …).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel


class ItemCard(QLabel):

    def __init__(self, size: int, config: dict, parent=None):
        super().__init__(parent)
        self._card_config = config
        self.selected = False
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.setStyleSheet(self._build_card_stylesheet())

    def setPixmap(self, pixmap: QPixmap) -> None:
        # QLabel paints pixmaps at natural size and clips to contentsRect
        # (inset by the stylesheet border). A pixmap larger than contentsRect
        # gets its outer ring hidden — then a VideoView overlay sized to the
        # same label rect renders that previously-hidden ring as visible
        # video, producing the "jump" on hover. Pre-scale to fit inside
        # contentsRect so the pixmap and any overlay both live at the same
        # aligned rect.
        if not pixmap.isNull():
            content = self.contentsRect()
            cw, ch = content.width(), content.height()
            if cw > 0 and ch > 0 and (pixmap.width() > cw or pixmap.height() > ch):
                pixmap = pixmap.scaled(
                    cw, ch,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        super().setPixmap(pixmap)

    # -- Style hooks (override in subclasses) -----------------------------

    def _style_selector(self) -> str:
        """CSS selector used in the stylesheet (e.g. ``"QLabel"``)."""
        return "QLabel"

    def _style_bg(self) -> str:
        return self._card_config.get("placeholder_color", "#1a1a1a")

    def _style_hover_bg(self) -> str | None:
        return None

    # -- Core styling -----------------------------------------------------

    def _build_card_stylesheet(self) -> str:
        sel = self._style_selector()
        bw = self._card_config.get("border_width", 1)
        bc = (self._card_config.get("select_border_color", "orange")
              if self.selected else "transparent")
        hc = self._card_config.get("hover_border_color", "#2d59b6")
        bg = self._style_bg()
        hover_bg = self._style_hover_bg()

        hover_extra = f" background: {hover_bg};" if hover_bg else ""

        hover_bc = bc if self.selected else hc
        return (
            f"{sel} {{ background-color: {bg}; border: {bw}px solid {bc}; }}"
            f" {sel}:hover {{ border: {bw}px solid {hover_bc};{hover_extra} }}"
        )

    def setSelected(self, selected: bool):
        if self.selected != selected:
            self.selected = selected
            self.setStyleSheet(self._build_card_stylesheet())
