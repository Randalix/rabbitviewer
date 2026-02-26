# core/priority.py
"""Qt-free enums and dataclasses shared across daemon, plugins, and scripts."""
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum, Enum, auto
from typing import Callable, Any, Optional, List, Set, Generator, Tuple


class Priority(IntEnum):
    BACKGROUND_SCAN = 10
    ORPHAN_SCAN = 15
    CONTENT_HASH = 20
    LOW = 30
    GUI_REQUEST_LOW = 40
    NORMAL = 50
    HIGH = 70
    GUI_REQUEST = 90
    FULLRES_REQUEST = 95
    SHUTDOWN = 999

    @classmethod
    def _missing_(cls, value):
        """Allow intermediate heatmap priorities (e.g. 85, 75, 65).

        Creates a pseudo-member so Priority(85) works and .name returns
        a human-readable string like 'PRIORITY_85'.
        """
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj._name_ = f"PRIORITY_{value}"
        return obj


class TaskState(IntEnum):
    PENDING = 1
    RUNNING = 2
    QUEUED = 3
    COMPLETED = 4
    FAILED = 5


class TaskType(Enum):
    SIMPLE = auto()
    GENERATOR = auto()


@dataclass
class SourceJob:
    priority: Priority
    job_id: str
    generator: Generator[Any, None, None]
    task_factory: Callable[[Any, Priority], List['RenderTask']]
    create_tasks: bool = True
    task_priority: Optional[Priority] = None
    on_complete: Optional[Callable] = field(default=None, compare=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, compare=False)

    def __lt__(self, other):
        # why: inverted so higher Priority value wins in a min-heap queue
        return self.priority > other.priority

    def cancel(self):
        self._cancel_event.set()

    def is_cancelled(self):
        return self._cancel_event.is_set()


@dataclass
class RenderTask:
    task_id: str = field(compare=False)
    func: Callable = field(compare=False)
    priority: Priority = field(compare=False)

    timestamp: float = field(compare=False, default_factory=time.perf_counter)
    args: tuple = field(compare=False, default_factory=tuple)
    kwargs: dict = field(compare=False, default_factory=dict)
    task_type: TaskType = field(compare=False, default=TaskType.SIMPLE)
    dependencies: Set[str] = field(compare=False, default_factory=set)
    dependents: Set[str] = field(compare=False, default_factory=set)
    state: TaskState = field(compare=False, default=TaskState.PENDING)
    worker_thread_id: Optional[int] = field(compare=False, default=None)
    on_complete_callback: Optional[Callable] = field(compare=False, default=None)
    is_active: bool = field(compare=False, default=True)
    cancel_event: Optional[threading.Event] = field(compare=False, default=None)

    def __lt__(self, other):
        # why: inverted so higher Priority value wins; ties broken by insertion order
        if not isinstance(other, RenderTask):
            return NotImplemented
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.timestamp < other.timestamp


def xmp_sidecar_path(image_path: str) -> str:
    """Return the conventional XMP sidecar path for an image: photo.jpg -> photo.jpg.xmp"""
    return image_path + ".xmp"


@dataclass(frozen=True, eq=False)
class ImageEntry:
    """Structural identity for an image and its sidecar files.

    Identity is ``(path, variant)`` only — two entries with the same path but
    different sidecar discovery states compare equal.  This prevents set/dict
    bugs when sidecars appear mid-session.
    """
    path: str
    sidecars: Tuple[str, ...] = ()
    variant: Optional[str] = None  # future: virtual copy label

    def __eq__(self, other):
        if not isinstance(other, ImageEntry):
            return NotImplemented
        return self.path == other.path and self.variant == other.variant

    def __hash__(self):
        return hash((self.path, self.variant))

    @staticmethod
    def from_path(image_path: str) -> 'ImageEntry':
        """Construct an ImageEntry, auto-discovering the XMP sidecar."""
        xmp = xmp_sidecar_path(image_path)
        sidecars = (xmp,) if os.path.exists(xmp) else ()
        return ImageEntry(path=image_path, sidecars=sidecars)

    @staticmethod
    def from_dict(d) -> 'ImageEntry':
        """Construct from a dict or bare str (coerces str → ImageEntry(path=str))."""
        if isinstance(d, str):
            return ImageEntry(path=d)
        if isinstance(d, ImageEntry):
            return d
        return ImageEntry(
            path=d["path"],
            sidecars=tuple(d.get("sidecars", ())),
            variant=d.get("variant"),
        )

    def to_dict(self) -> dict:
        """Serialize to a dict suitable for JSON / protocol messages."""
        d: dict = {"path": self.path}
        if self.sidecars:
            d["sidecars"] = list(self.sidecars)
        if self.variant is not None:
            d["variant"] = self.variant
        return d

    def all_files(self) -> Tuple[str, ...]:
        """Return the image path plus all sidecar paths."""
        return (self.path,) + self.sidecars
