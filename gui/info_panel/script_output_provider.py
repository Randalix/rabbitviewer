from typing import List

from .content_provider import ContentProvider, Section


class ScriptOutputProvider(ContentProvider):
    """Displays structured output from scripts. Infrastructure TBD."""

    def __init__(self):
        self._last_output: dict = {}

    @property
    def provider_name(self) -> str:
        return "Script Output"

    def get_sections(self, image_path: str) -> List[Section]:
        output = self._last_output.get(image_path)
        if not output:
            return [Section("Output", [("", "No script output available")])]
        return [Section("Script Output", output)]

    def receive_output(self, image_path: str, key: str, value: str):
        """Accumulator for future ScriptAPI.emit_output()."""
        if image_path not in self._last_output:
            self._last_output[image_path] = []
        self._last_output[image_path].append((key, value))
