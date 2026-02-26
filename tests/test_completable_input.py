"""Tests for the generic CompletableInput widget."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gui.components.completable_input import CompletableInput, SEPARATOR


class TestTokenParsing:

    def test_parse_simple(self):
        w = CompletableInput(separator=",")
        w.setText("alpha, beta, gamma")
        assert w.get_values() == ["alpha", "beta", "gamma"]

    def test_parse_deduplicates(self):
        w = CompletableInput(separator=",")
        w.setText("a, b, a, c, b")
        assert w.get_values() == ["a", "b", "c"]

    def test_parse_strips_whitespace(self):
        w = CompletableInput(separator=",")
        w.setText("  foo ,  bar  , baz  ")
        assert w.get_values() == ["foo", "bar", "baz"]

    def test_parse_empty(self):
        w = CompletableInput(separator=",")
        w.setText("")
        assert w.get_values() == []

    def test_parse_trailing_separator(self):
        w = CompletableInput(separator=",")
        w.setText("a, b,")
        assert w.get_values() == ["a", "b"]

    def test_parse_ignores_separator_string(self):
        w = CompletableInput(separator=",")
        w.setText(f"a, {SEPARATOR}, b")
        assert w.get_values() == ["a", "b"]

    def test_custom_separator(self):
        w = CompletableInput(separator=";")
        w.setText("x; y; z")
        assert w.get_values() == ["x", "y", "z"]


class TestSetValues:

    def test_roundtrip(self):
        w = CompletableInput(separator=",")
        w.set_values(["one", "two", "three"])
        assert w.get_values() == ["one", "two", "three"]

    def test_roundtrip_custom_separator(self):
        w = CompletableInput(separator=";")
        w.set_values(["a", "b"])
        assert w.get_values() == ["a", "b"]

    def test_set_empty(self):
        w = CompletableInput(separator=",")
        w.set_values([])
        assert w.get_values() == []


class TestSetCompletions:

    def test_stores_completions(self):
        w = CompletableInput(separator=",")
        w.set_completions(["alpha", "beta", "gamma"])
        assert w._completions == ["alpha", "beta", "gamma"]

    def test_completions_with_separator(self):
        w = CompletableInput(separator=",")
        items = ["dir_a", "dir_b", SEPARATOR, "global_c"]
        w.set_completions(items)
        assert w._completions == items


class TestCleanSeparators:

    def test_removes_leading(self):
        assert CompletableInput._clean_separators([SEPARATOR, "a"]) == ["a"]

    def test_removes_trailing(self):
        assert CompletableInput._clean_separators(["a", SEPARATOR]) == ["a"]

    def test_removes_consecutive(self):
        assert CompletableInput._clean_separators(
            ["a", SEPARATOR, SEPARATOR, "b"]
        ) == ["a", SEPARATOR, "b"]

    def test_keeps_valid(self):
        assert CompletableInput._clean_separators(
            ["a", SEPARATOR, "b"]
        ) == ["a", SEPARATOR, "b"]

    def test_all_separators(self):
        assert CompletableInput._clean_separators(
            [SEPARATOR, SEPARATOR]
        ) == []

    def test_empty(self):
        assert CompletableInput._clean_separators([]) == []


class TestTagInputCompat:
    """Verify TagInput subclass maintains backward-compatible API."""

    def test_tag_input_is_completable_input(self):
        from gui.components.tag_input import TagInput
        w = TagInput()
        assert isinstance(w, CompletableInput)

    def test_get_set_tags(self):
        from gui.components.tag_input import TagInput
        w = TagInput()
        w.set_tags(["sunset", "beach"])
        assert w.get_tags() == ["sunset", "beach"]

    def test_set_available_tags(self):
        from gui.components.tag_input import TagInput
        w = TagInput()
        w.set_available_tags(["local_a"], ["local_a", "global_b"])
        # global_b should appear after separator; local_a deduped
        assert "local_a" in w._completions
        assert "global_b" in w._completions
        assert SEPARATOR in w._completions

    def test_set_available_tags_no_separator_when_no_global(self):
        from gui.components.tag_input import TagInput
        w = TagInput()
        w.set_available_tags(["a", "b"], ["a", "b"])
        assert SEPARATOR not in w._completions
