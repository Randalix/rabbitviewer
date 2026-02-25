from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class Section:
    """A named group of key-value pairs for display."""
    title: str
    rows: List[Tuple[str, str]] = field(default_factory=list)


class ContentProvider(ABC):
    """Formats data into display sections for an InfoPanel."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short name for the window title."""

    @abstractmethod
    def get_sections(self, image_path: str) -> List[Section]:
        """Return sections for the given image. Must be instant (no I/O)."""

    def on_cleanup(self) -> None:
        """Optional teardown hook."""
