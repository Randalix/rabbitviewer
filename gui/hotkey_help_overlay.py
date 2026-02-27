import re
from typing import Dict, List, Tuple

from PySide6.QtWidgets import QWidget, QGraphicsDropShadowEffect, QApplication
from PySide6.QtCore import Qt, QSize, QEvent, QObject, QRectF, QSettings
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath, QMouseEvent, QFontMetrics

from config.hotkeys import HotkeyDefinition


_SETTINGS_KEY = "hotkey_help/show_at_startup"

# (category_label, action_names) — order defines display order
_CATEGORIES: List[Tuple[str, List[str]]] = [
    ("Navigation", [
        "next_image", "previous_image", "escape_picture_view", "close_or_quit",
    ]),
    ("View", [
        "toggle_inspector", "pin_inspector", "open_filter", "zoom_in", "zoom_out",
    ]),
    ("Selection", [
        "start_range_selection", "undo_selection", "redo_selection",
        "script:select_all", "script:invert_selection",
    ]),
    ("Ratings", []),  # filled dynamically from script:set_rating_*
    ("Menus", []),     # filled dynamically from menu:*
]

_RATING_PATTERN = re.compile(r"^script:set_rating_\d+$")


def _build_rows(definitions: Dict[str, HotkeyDefinition]) -> List[Tuple[str, list]]:
    """Return [(category, [(sequences, description), ...]), ...] for display."""
    known = set()
    for _, actions in _CATEGORIES:
        known.update(actions)

    rows_by_category: List[Tuple[str, list]] = []

    for cat_label, static_actions in _CATEGORIES:
        rows = []

        if cat_label == "Ratings":
            rating_defs = sorted(
                [d for name, d in definitions.items() if _RATING_PATTERN.match(name)],
                key=lambda d: d.action_name,
            )
            if rating_defs:
                all_seqs = []
                for d in rating_defs:
                    all_seqs.extend(d.sequences)
                    known.add(d.action_name)
                rows.append((all_seqs, "Set rating"))
        elif cat_label == "Menus":
            for name, d in sorted(definitions.items()):
                if name.startswith("menu:") and name not in known:
                    known.add(name)
                    rows.append((d.sequences, d.description or name[5:].replace("_", " ").title()))
        else:
            for action_name in static_actions:
                d = definitions.get(action_name)
                if d and d.sequences:
                    rows.append((d.sequences, d.description or action_name.replace("_", " ").title()))

        if rows:
            rows_by_category.append((cat_label, rows))

    other_rows = []
    for name, d in sorted(definitions.items()):
        if name not in known and name != "show_hotkey_help" and d.sequences:
            desc = d.description or name.replace("_", " ").replace("script:", "").title()
            other_rows.append((d.sequences, desc))
    if other_rows:
        rows_by_category.append(("Other", other_rows))

    return rows_by_category


def show_at_startup() -> bool:
    """Return True if the overlay should auto-show on startup (default: True)."""
    settings = QSettings("RabbitViewer", "HotkeyHelp")
    return settings.value(_SETTINGS_KEY, True, type=bool)


class HotkeyHelpOverlay(QWidget):
    """Floating overlay that lists all active keyboard shortcuts."""

    _PADDING = 24
    _ROW_HEIGHT = 28
    _SECTION_GAP = 12
    _TITLE_HEIGHT = 36
    _FOOTER_HEIGHT = 32
    _CHECKBOX_SIZE = 14
    _KEY_GAP = 6
    _BADGE_HPAD = 8
    _BADGE_VPAD = 2
    _FONT_SIZE = 13
    _BG_COLOR = QColor(30, 30, 30, 235)
    _KEY_BG = QColor(70, 70, 70)
    _KEY_FG = QColor(220, 200, 100)
    _LABEL_FG = QColor(220, 220, 220)
    _HEADER_FG = QColor(140, 180, 255)
    _TITLE_FG = QColor(255, 255, 255)
    _HINT_FG = QColor(140, 140, 140)
    _CHECKBOX_BORDER = QColor(140, 140, 140)
    _CHECKBOX_CHECK = QColor(220, 200, 100)
    _CORNER_RADIUS = 10

    def __init__(self, parent: QWidget, definitions: Dict[str, HotkeyDefinition],
                 trigger_key: str = "?"):
        super().__init__(parent)
        self._is_open = False
        self._sections = _build_rows(definitions)
        self._trigger_key = trigger_key
        self._show_at_startup = show_at_startup()
        self._checkbox_rect = QRectF()  # set during paint

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def toggle(self):
        if self._is_open:
            self._close()
        else:
            self._open()

    def _open(self):
        self._resize_to_fit()
        self._position_center()
        self._is_open = True
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)
        self.show()
        self.raise_()
        self.update()

    def _close(self):
        if not self._is_open:
            return
        self._is_open = False
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)
        self.hide()

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _resize_to_fit(self):
        font = QFont("monospace", self._FONT_SIZE)
        font.setStyleHint(QFont.Monospace)
        fm = QFontMetrics(font)

        max_keys_w = 0
        max_desc_w = 0
        total_rows = 0

        for _, rows in self._sections:
            total_rows += 1  # section header
            for seqs, desc in rows:
                total_rows += 1
                keys_w = self._measure_keys_width(fm, seqs)
                max_keys_w = max(max_keys_w, keys_w)
                desc_w = fm.horizontalAdvance(desc)
                max_desc_w = max(max_desc_w, desc_w)

        col_gap = 20
        w = self._PADDING * 2 + max_keys_w + col_gap + max_desc_w
        w = max(int(w), 300)
        w = min(w, 600)

        section_gaps = (len(self._sections) - 1) * self._SECTION_GAP if self._sections else 0
        h = (self._PADDING * 2 + self._TITLE_HEIGHT + section_gaps
             + total_rows * self._ROW_HEIGHT
             + self._SECTION_GAP + self._FOOTER_HEIGHT)
        h = min(h, 800)

        self.setFixedSize(QSize(w, int(h)))
        self._keys_col_width = max_keys_w

    def _measure_keys_width(self, fm: QFontMetrics, sequences: List[str]) -> int:
        w = 0
        for i, seq in enumerate(sequences):
            if i > 0:
                w += self._KEY_GAP
            text_w = fm.horizontalAdvance(seq)
            w += text_w + self._BADGE_HPAD * 2
        return w

    def _position_center(self):
        parent = self.parentWidget()
        if parent:
            px = parent.width() // 2 - self.width() // 2
            py = parent.height() // 2 - self.height() // 2
            self.move(parent.mapToGlobal(parent.rect().topLeft()).x() + px,
                      parent.mapToGlobal(parent.rect().topLeft()).y() + py)

    # ------------------------------------------------------------------
    # Event filter
    # ------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if not self._is_open:
            return False

        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True

        if event.type() == QEvent.Type.KeyPress:
            self._close()
            return True

        if event.type() == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)
                # Toggle checkbox if clicked
                if self._checkbox_rect.contains(local_pos.toPointF()):
                    self._show_at_startup = not self._show_at_startup
                    settings = QSettings("RabbitViewer", "HotkeyHelp")
                    settings.setValue(_SETTINGS_KEY, self._show_at_startup)
                    self.update()
                    return True
                if not self.geometry().contains(global_pos):
                    self._close()
                    return True

        return False

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        path = QPainterPath()
        path.addRoundedRect(self.rect().toRectF(), self._CORNER_RADIUS, self._CORNER_RADIUS)
        painter.fillPath(path, self._BG_COLOR)

        font = QFont("monospace", self._FONT_SIZE)
        font.setStyleHint(QFont.Monospace)
        painter.setFont(font)
        fm = QFontMetrics(font)

        y = self._PADDING

        # Title
        title_font = QFont("monospace", self._FONT_SIZE + 2, QFont.Bold)
        title_font.setStyleHint(QFont.Monospace)
        painter.setFont(title_font)
        painter.setPen(self._TITLE_FG)
        painter.drawText(self._PADDING, y, self.width() - self._PADDING * 2,
                         self._TITLE_HEIGHT, Qt.AlignVCenter | Qt.AlignLeft,
                         "Keyboard Shortcuts")
        y += self._TITLE_HEIGHT

        painter.setFont(font)

        for section_idx, (cat_label, rows) in enumerate(self._sections):
            if section_idx > 0:
                y += self._SECTION_GAP

            # Section header
            header_font = QFont("monospace", self._FONT_SIZE - 1, QFont.Bold)
            header_font.setStyleHint(QFont.Monospace)
            painter.setFont(header_font)
            painter.setPen(self._HEADER_FG)
            painter.drawText(self._PADDING, y, self.width() - self._PADDING * 2,
                             self._ROW_HEIGHT, Qt.AlignVCenter | Qt.AlignLeft,
                             cat_label.upper())
            y += self._ROW_HEIGHT
            painter.setFont(font)

            for seqs, desc in rows:
                kx = self._PADDING
                for i, seq in enumerate(seqs):
                    if i > 0:
                        kx += self._KEY_GAP
                    text_w = fm.horizontalAdvance(seq)
                    badge_w = text_w + self._BADGE_HPAD * 2
                    badge_h = self._ROW_HEIGHT - self._BADGE_VPAD * 2 - 4
                    badge_y = y + (self._ROW_HEIGHT - badge_h) // 2
                    badge_path = QPainterPath()
                    badge_path.addRoundedRect(kx, badge_y, badge_w, badge_h, 4, 4)
                    painter.fillPath(badge_path, self._KEY_BG)

                    painter.setPen(self._KEY_FG)
                    painter.drawText(int(kx), y, int(badge_w), self._ROW_HEIGHT,
                                     Qt.AlignCenter, seq)
                    kx += badge_w

                desc_x = self._PADDING + self._keys_col_width + 20
                painter.setPen(self._LABEL_FG)
                painter.drawText(int(desc_x), y, self.width() - int(desc_x) - self._PADDING,
                                 self._ROW_HEIGHT, Qt.AlignVCenter | Qt.AlignLeft, desc)

                y += self._ROW_HEIGHT

        # Footer: hint + checkbox
        y += self._SECTION_GAP
        small_font = QFont("monospace", self._FONT_SIZE - 2)
        small_font.setStyleHint(QFont.Monospace)
        painter.setFont(small_font)
        small_fm = QFontMetrics(small_font)

        # Hint text on left
        hint = f"Press  {self._trigger_key}  to toggle this help"
        painter.setPen(self._HINT_FG)
        painter.drawText(self._PADDING, y, self.width() // 2, self._FOOTER_HEIGHT,
                         Qt.AlignVCenter | Qt.AlignLeft, hint)

        # Checkbox + label on right
        cb_label = "Don't show at startup"
        label_w = small_fm.horizontalAdvance(cb_label)
        cb_total_w = self._CHECKBOX_SIZE + 6 + label_w
        cb_x = self.width() - self._PADDING - cb_total_w
        cb_y = y + (self._FOOTER_HEIGHT - self._CHECKBOX_SIZE) // 2

        # Draw checkbox box
        cb_rect = QRectF(cb_x, cb_y, self._CHECKBOX_SIZE, self._CHECKBOX_SIZE)
        self._checkbox_rect = QRectF(cb_x - 4, y, cb_total_w + 8, self._FOOTER_HEIGHT)
        cb_path = QPainterPath()
        cb_path.addRoundedRect(cb_rect, 2, 2)
        painter.setPen(self._CHECKBOX_BORDER)
        painter.drawPath(cb_path)

        if not self._show_at_startup:
            # Draw checkmark
            painter.setPen(self._CHECKBOX_CHECK)
            cx, cy = cb_rect.center().x(), cb_rect.center().y()
            s = self._CHECKBOX_SIZE * 0.3
            painter.drawLine(int(cx - s), int(cy), int(cx - s * 0.3), int(cy + s * 0.7))
            painter.drawLine(int(cx - s * 0.3), int(cy + s * 0.7), int(cx + s), int(cy - s * 0.5))

        # Checkbox label
        painter.setPen(self._HINT_FG)
        painter.drawText(int(cb_x + self._CHECKBOX_SIZE + 6), y,
                         label_w + 4, self._FOOTER_HEIGHT,
                         Qt.AlignVCenter | Qt.AlignLeft, cb_label)

        painter.end()
