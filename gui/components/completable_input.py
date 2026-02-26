"""Generic multi-token QLineEdit with detached QCompleter popup.

Supports comma-separated (or custom separator) token input with
substring-match autocomplete.  The QCompleter is NOT attached via
setCompleter() — the popup is managed manually to avoid Qt's
single-value assumptions.

Subclass this for domain-specific completion (see TagInput).
"""

from PySide6.QtWidgets import QLineEdit, QCompleter
from PySide6.QtCore import Signal, Qt, QStringListModel


SEPARATOR = "──────────"


class CompletableInput(QLineEdit):
    """Token-based input with autocomplete popup.

    Signals:
        values_changed(list): Emitted when the parsed token list changes.
        confirmed(): Emitted on Enter — the parent dialog should close/confirm.
    """

    values_changed = Signal(list)
    confirmed = Signal()

    def __init__(self, separator: str = ",", placeholder: str = "", parent=None):
        super().__init__(parent)
        self._separator = separator
        if placeholder:
            self.setPlaceholderText(placeholder)

        self._completions: list[str] = []

        # Standalone completer — NOT attached via setCompleter().
        self._completer_model = QStringListModel(self)
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setWidget(self)

        self._completer.popup().clicked.connect(self._on_popup_clicked)
        self.textEdited.connect(self._on_text_edited)

    # ── Public API ──────────────────────────────────────────────────────

    def set_completions(self, items: list[str]) -> None:
        """Sets the flat completion candidate list.

        The list may contain SEPARATOR strings for visual grouping.
        Caller is responsible for ordering/grouping.
        """
        self._completions = list(items)
        self._refresh_completer()

    def get_values(self) -> list[str]:
        """Returns the current tokens as a deduplicated list."""
        return self._parse_tokens(self.text())

    def set_values(self, values: list[str]) -> None:
        """Populates the field with a list of tokens."""
        joiner = self._separator + " "
        self.setText(joiner.join(values))

    # ── Internals ───────────────────────────────────────────────────────

    def _parse_tokens(self, text: str) -> list[str]:
        seen = set()
        result = []
        for part in text.split(self._separator):
            token = part.strip()
            if token and token != SEPARATOR and token not in seen:
                seen.add(token)
                result.append(token)
        return result

    def _current_prefix(self) -> str:
        """Returns the text after the last separator (the token being typed)."""
        text = self.text()
        cursor = self.cursorPosition()
        before_cursor = text[:cursor]
        last_sep = before_cursor.rfind(self._separator)
        return before_cursor[last_sep + len(self._separator):].strip()

    def _refresh_completer(self) -> None:
        """Rebuilds the popup word list based on current prefix and entered values."""
        prefix = self._current_prefix().lower()
        already = set(t.lower() for t in self._parse_tokens(self.text()))

        words = [c for c in self._completions
                 if c == SEPARATOR or (c.lower() not in already and prefix in c.lower())]

        # Strip leading/trailing/consecutive separators.
        words = self._clean_separators(words)

        self._completer_model.setStringList(words)

        popup = self._completer.popup()
        if words and prefix:
            self._completer.complete()
            popup.setCurrentIndex(self._completer_model.index(-1, 0))
        else:
            popup.hide()

    @staticmethod
    def _clean_separators(words: list[str]) -> list[str]:
        """Removes leading, trailing, and consecutive separator entries."""
        result = []
        for w in words:
            if w == SEPARATOR:
                if not result or result[-1] == SEPARATOR:
                    continue
                result.append(w)
            else:
                result.append(w)
        if result and result[-1] == SEPARATOR:
            result.pop()
        return result

    def _popup_visible(self) -> bool:
        return self._completer.popup().isVisible()

    def _highlighted_completion(self) -> str | None:
        popup = self._completer.popup()
        index = popup.currentIndex()
        if index.isValid():
            text = index.data()
            if text and text != SEPARATOR:
                return text
        return None

    def _first_match(self) -> str | None:
        for word in self._completer_model.stringList():
            if word != SEPARATOR:
                return word
        return None

    def _insert_completion(self, completion: str) -> None:
        """Replaces the current token with the selected completion."""
        if completion == SEPARATOR:
            return

        text = self.text()
        cursor = self.cursorPosition()
        before_cursor = text[:cursor]
        after_cursor = text[cursor:]

        last_sep = before_cursor.rfind(self._separator)
        prefix = before_cursor[:last_sep + len(self._separator)] if last_sep >= 0 else ""
        if prefix and not prefix.endswith(" "):
            prefix += " "

        suffix = self._separator + " "
        new_text = prefix + completion + suffix + after_cursor.lstrip(self._separator + " ")
        self.setText(new_text)
        self.setCursorPosition(len(prefix) + len(completion) + len(suffix))
        self.values_changed.emit(self.get_values())

    def _on_text_edited(self, text: str) -> None:
        self._refresh_completer()
        if text.endswith(self._separator):
            self.values_changed.emit(self.get_values())

    def _on_popup_clicked(self, index) -> None:
        text = index.data()
        if text and text != SEPARATOR:
            self._insert_completion(text)
            self._refresh_completer()
        self.setFocus()

    # ── Key handling ────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        key = event.key()

        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self._popup_visible():
                self._completer.popup().hide()
            self.values_changed.emit(self.get_values())
            self.confirmed.emit()
            event.ignore()
            return

        if key == Qt.Key_Tab:
            target = self._highlighted_completion() or self._first_match()
            if target:
                self._insert_completion(target)
                self._refresh_completer()
            return

        if key == Qt.Key_Escape:
            if self._popup_visible():
                self._completer.popup().hide()
                return
            super().keyPressEvent(event)
            return

        if key in (Qt.Key_Up, Qt.Key_Down) and self._popup_visible():
            popup = self._completer.popup()
            current = popup.currentIndex()
            row = current.row() if current.isValid() else -1
            count = self._completer_model.rowCount()

            if key == Qt.Key_Down:
                new_row = row + 1 if row < count - 1 else 0
            else:
                new_row = row - 1 if row > 0 else count - 1

            candidate = self._completer_model.stringList()[new_row]
            if candidate == SEPARATOR:
                new_row += 1 if key == Qt.Key_Down else -1
                new_row = max(0, min(new_row, count - 1))

            new_index = self._completer_model.index(new_row, 0)
            popup.setCurrentIndex(new_index)
            popup.scrollTo(new_index)
            return

        super().keyPressEvent(event)
