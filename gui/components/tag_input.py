"""Comma-separated tag input with two-tier autocomplete.

Used by both the FilterDialog (for tag filtering) and the TagEditorDialog
(for tag assignment).  Emits `tags_changed` whenever the parsed tag list changes.
"""

from PySide6.QtWidgets import QLineEdit, QCompleter
from PySide6.QtCore import Signal, Qt, QStringListModel


_SEPARATOR = "──────────"


class TagInput(QLineEdit):
    """A QLineEdit that accepts comma-separated tags with autocomplete."""

    tags_changed = Signal(list)  # emits the parsed list of tag strings
    confirmed = Signal()  # Enter pressed (even when completer consumed the key)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Enter tags separated by commas...")

        self._completer_model = QStringListModel()
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.activated.connect(self._on_completer_activated)
        self.setCompleter(self._completer)

        self._directory_tags: list[str] = []
        self._global_tags: list[str] = []

        self.textEdited.connect(self._on_text_edited)

    # ── Public API ──────────────────────────────────────────────────────

    def set_available_tags(self, directory_tags: list[str], global_tags: list[str]) -> None:
        """Populates the autocomplete model with two-tier tag lists."""
        self._directory_tags = sorted(set(directory_tags))
        self._global_tags = sorted(set(global_tags))
        self._refresh_completer()

    def get_tags(self) -> list[str]:
        """Returns the current comma-separated values as a deduplicated list."""
        return self._parse_tags(self.text())

    def set_tags(self, tags: list[str]) -> None:
        """Populates the field with a list of tags."""
        self.setText(", ".join(tags))

    # ── Internals ───────────────────────────────────────────────────────

    def _parse_tags(self, text: str) -> list[str]:
        seen = set()
        result = []
        for part in text.split(","):
            tag = part.strip()
            if tag and tag != _SEPARATOR and tag not in seen:
                seen.add(tag)
                result.append(tag)
        return result

    def _current_prefix(self) -> str:
        """Returns the text after the last comma (the token being typed)."""
        text = self.text()
        cursor = self.cursorPosition()
        before_cursor = text[:cursor]
        last_comma = before_cursor.rfind(",")
        return before_cursor[last_comma + 1:].strip()

    def _refresh_completer(self) -> None:
        """Rebuilds the completer word list based on the current prefix."""
        prefix = self._current_prefix().lower()
        already = set(t.lower() for t in self._parse_tags(self.text()))

        dir_matches = [t for t in self._directory_tags
                       if t.lower() not in already and prefix in t.lower()]
        # Global tags that are NOT already in the directory list
        dir_set = set(t.lower() for t in self._directory_tags)
        global_matches = [t for t in self._global_tags
                          if t.lower() not in already and t.lower() not in dir_set
                          and prefix in t.lower()]

        words = list(dir_matches)
        if dir_matches and global_matches:
            words.append(_SEPARATOR)
        words.extend(global_matches)

        self._completer_model.setStringList(words)

    def _on_text_edited(self, _text: str) -> None:
        self._refresh_completer()
        # Only emit on explicit commit (comma typed at the end or Enter).
        # Check if the last character typed was a comma:
        if _text.endswith(","):
            self.tags_changed.emit(self.get_tags())

    def _on_completer_activated(self, completion: str) -> None:
        """Completer accepted via Enter — insert and confirm."""
        self._insert_completion(completion)
        self.confirmed.emit()

    def _insert_completion(self, completion: str) -> None:
        """Replaces the current token with the selected completion."""
        if completion == _SEPARATOR:
            return

        text = self.text()
        cursor = self.cursorPosition()
        before_cursor = text[:cursor]
        after_cursor = text[cursor:]

        last_comma = before_cursor.rfind(",")
        prefix = before_cursor[:last_comma + 1] if last_comma >= 0 else ""
        if prefix and not prefix.endswith(" "):
            prefix += " "

        new_text = prefix + completion + ", " + after_cursor.lstrip(", ")
        self.setText(new_text)
        self.setCursorPosition(len(prefix) + len(completion) + 2)
        self.tags_changed.emit(self.get_tags())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.tags_changed.emit(self.get_tags())
            self.confirmed.emit()
            # Don't consume — let the parent dialog handle close/accept.
            event.ignore()
            return
        if event.key() == Qt.Key_Tab:
            # Shell-style tab completion: accept the first match.
            self._refresh_completer()
            words = self._completer_model.stringList()
            for word in words:
                if word != _SEPARATOR:
                    self._insert_completion(word)
                    break
            return  # consume the event to prevent focus change
        super().keyPressEvent(event)
