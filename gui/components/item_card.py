"""Base card widget with config-driven selection/hover borders.

Subclasses override ``_style_*()`` hooks to customise appearance while
keeping border, selection, and hover behaviour identical across all
card-based views (thumbnail grid, face palette, …).
"""

from PySide6.QtCore import Qt
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

    # -- Style hooks (override in subclasses) -----------------------------

    def _style_selector(self) -> str:
        """CSS selector used in the stylesheet (e.g. ``"QLabel"``)."""
        return "QLabel"

    def _style_bg(self) -> str:
        return self._card_config.get("placeholder_color", "#1a1a1a")

    def _style_hover_bg(self) -> str | None:
        return None

    def _style_border_radius(self) -> int:
        return 0

    # -- Core styling -----------------------------------------------------

    def _build_card_stylesheet(self) -> str:
        sel = self._style_selector()
        bw = self._card_config.get("border_width", 1)
        bc = (self._card_config.get("select_border_color", "orange")
              if self.selected else "transparent")
        hc = self._card_config.get("hover_border_color", "#2d59b6")
        bg = self._style_bg()
        hover_bg = self._style_hover_bg()
        br = self._style_border_radius()

        radius = f" border-radius: {br}px;" if br else ""
        hover_extra = f" background: {hover_bg};" if hover_bg else ""

        hover_bc = bc if self.selected else hc
        return (
            f"{sel} {{ background-color: {bg}; border: {bw}px solid {bc};{radius} }}"
            f" {sel}:hover {{ border: {bw}px solid {hover_bc};{hover_extra} }}"
        )

    def setSelected(self, selected: bool):
        if self.selected != selected:
            self.selected = selected
            self.setStyleSheet(self._build_card_stylesheet())
