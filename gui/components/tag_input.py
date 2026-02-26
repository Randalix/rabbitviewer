"""Tag-specific completion input — thin wrapper around CompletableInput.

Provides two-tier autocomplete (directory tags first, then global tags)
and re-exports signals with tag-specific names for backward compatibility
with TagEditorDialog and TagFilterDialog.
"""

from PySide6.QtCore import Signal

from gui.components.completable_input import CompletableInput, SEPARATOR


class TagInput(CompletableInput):
    """Comma-separated tag input with two-tier directory/global autocomplete."""

    tags_changed = Signal(list)  # alias for values_changed

    def __init__(self, parent=None):
        super().__init__(
            separator=",",
            placeholder="Enter tags separated by commas...",
            parent=parent,
        )
        self._directory_tags: list[str] = []
        self._global_tags: list[str] = []

        # Forward generic signal to tag-specific name.
        self.values_changed.connect(self.tags_changed.emit)

    # ── Public API (tag-specific) ───────────────────────────────────────

    def set_available_tags(self, directory_tags: list[str], global_tags: list[str]) -> None:
        """Populates the autocomplete model with two-tier tag lists."""
        self._directory_tags = sorted(set(directory_tags))
        self._global_tags = sorted(set(global_tags))
        self._rebuild_completions()

    def get_tags(self) -> list[str]:
        """Returns the current tags (alias for get_values)."""
        return self.get_values()

    def set_tags(self, tags: list[str]) -> None:
        """Populates the field with tags (alias for set_values)."""
        self.set_values(tags)

    # ── Internals ───────────────────────────────────────────────────────

    def _rebuild_completions(self) -> None:
        """Builds the two-tier list: directory tags, separator, global-only tags."""
        dir_set = set(t.lower() for t in self._directory_tags)
        global_only = [t for t in self._global_tags if t.lower() not in dir_set]

        items = list(self._directory_tags)
        if items and global_only:
            items.append(SEPARATOR)
        items.extend(global_only)
        self.set_completions(items)
