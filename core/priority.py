# core/priority.py
"""Qt-free enums and dataclasses shared across daemon, plugins, and scripts."""
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum, Enum, auto
from typing import Callable, Any, Optional, List, Set, Generator


class Priority(IntEnum):
    BACKGROUND_SCAN = 10
    CONTENT_HASH = 20
    LOW = 30
    GUI_REQUEST_LOW = 40
    NORMAL = 50
    HIGH = 70
    GUI_REQUEST = 90
    FULLRES_REQUEST = 95
    SHUTDOWN = 999


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
