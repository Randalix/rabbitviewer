"""Startup task scheduler — priority-ordered background initialization.

Tasks are grouped into integer phases. Tasks within a phase run in parallel
(via a thread pool). All tasks in phase N must complete before any task in
phase N+1 begins — phase numbers act as barriers.

Each task receives the accumulated results from all previously completed tasks
as a read-only snapshot dict, allowing downstream tasks to consume upstream
outputs without shared-state races (earlier phase is fully settled before the
next begins).

The scheduler is Qt-free. Pass an ``on_task_done`` callback to receive
completion notifications from the worker threads; bridge to the Qt main thread
there (e.g. emit a Signal).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class Phase:
    """Startup phase constants. Lower = earlier / higher priority."""

    PARALLEL_INIT = 10  # Independent tasks (DB init, plugin loading) — run in parallel
    CORE = 20           # ThumbnailManager construction (depends on DB)
    SERVICE = 30        # ThumbnailService wiring (depends on CORE + plugins)
    SUPPORTING = 50     # Watchdog start, pending-write recovery
    LAZY = 80           # Non-essential deferred work (scripts, ONNX prewarm)


@dataclass
class StartupTask:
    name: str
    phase: int
    fn: Callable[[Dict[str, Any]], Any]


class StartupScheduler:
    """Priority-ordered startup task runner.

    Usage::

        def _on_done(name, result):
            # called on worker thread — emit a Qt signal here
            bridge.task_done.emit(name, result)

        sched = StartupScheduler(on_task_done=_on_done)
        sched.add("db",      Phase.PARALLEL_INIT, lambda r: init_db())
        sched.add("plugins", Phase.PARALLEL_INIT, lambda r: load_plugins())
        sched.add("core",    Phase.CORE,           lambda r: build_tm(r["db"]))
        sched.run()
    """

    def __init__(self, on_task_done: Callable[[str, Any], None]):
        self._tasks: List[StartupTask] = []
        self._on_done = on_task_done

    def add(self, name: str, phase: int, fn: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a startup task."""
        self._tasks.append(StartupTask(name=name, phase=phase, fn=fn))

    def run(self) -> None:
        """Start the scheduler in a daemon thread."""
        threading.Thread(
            target=self._worker, daemon=True, name="StartupScheduler"
        ).start()

    def _worker(self) -> None:
        results: Dict[str, Any] = {}
        phases = sorted(set(t.phase for t in self._tasks))

        # One pool shared across all phases so threads can be reused.
        with ThreadPoolExecutor(thread_name_prefix="Startup") as pool:
            for phase in phases:
                phase_tasks = [t for t in self._tasks if t.phase == phase]
                logger.debug(
                    "[startup] phase %d: %s",
                    phase,
                    [t.name for t in phase_tasks],
                )
                snapshot = dict(results)  # stable snapshot for this phase
                futures = {pool.submit(t.fn, snapshot): t for t in phase_tasks}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        logger.exception("[startup] task %r failed", task.name)
                        result = None
                    results[task.name] = result
                    self._on_done(task.name, result)
                # Phase barrier: all futures in this phase finish before the loop
                # advances to the next phase, so phase N+1 tasks see the full
                # snapshot of phase N results.

        logger.debug("[startup] all phases complete")
