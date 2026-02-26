import threading
from collections import deque
from queue import PriorityQueue, Empty, Full, Queue
import time
import logging
from PySide6.QtCore import QObject, Signal
from typing import Callable, Any, Optional, Dict, List, Set, Generator
from core.priority import Priority, TaskState, TaskType, SourceJob, RenderTask  # noqa: F401

logger = logging.getLogger(__name__)

class RenderManager(QObject):
    """
    Manages background tasks with a dependency graph, a fixed number of workers,
    a priority queue, and preemption logic for orchestrating complex workflows.
    """
    taskCompleted = Signal()
    shutdownFinished = Signal()

    def __init__(self, num_workers: int = 4):
        super().__init__()
        self.num_workers = num_workers
        self._running = False
        self._shutting_down = threading.Event()
        # --- The Unified Graph is the single source of truth ---
        self.task_graph: Dict[str, RenderTask] = {}
        self.graph_lock = threading.Lock()

        # --- Single Queue for all runnable tasks (simple and generator) ---
        self.task_queue = PriorityQueue[RenderTask]()
        self.worker_threads: List[threading.Thread] = []

        self.active_tasks: Dict[int, RenderTask] = {}
        self.active_tasks_lock = threading.Lock()
        self.task_callbacks: Dict[str, List[Callable]] = {}
        self.task_callbacks_lock = threading.Lock()

        # Tracking for active jobs
        self.active_jobs: Dict[str, SourceJob] = {}
        self.active_jobs_lock = threading.Lock()
        
        # --- Notification Queue for outbound messages to the SocketServer ---
        # Bounded to prevent unbounded memory growth under high throughput.
        self.notification_queue: Queue = Queue(maxsize=5000)

    def start(self):
        if self._running:
            return
        logger.info(f"RenderManager: Starting with {self.num_workers} workers.")
        self._running = True

        for i in range(self.num_workers):
            worker = threading.Thread(target=self._worker_loop, args=(i,), daemon=True)
            worker.name = f"UnifiedWorker-{i}"
            self.worker_threads.append(worker)
            worker.start()
        
        # The pipeline processor thread is removed, as SourceJobs are now cooperative tasks.

    def get_all_job_ids(self) -> List[str]:
        with self.active_jobs_lock:
            return list(self.active_jobs.keys())

    def cancel_task(self, task_id: str) -> bool:
        """Cooperatively cancel a task (set cancel_event + mark inactive)."""
        with self.graph_lock:
            task = self.task_graph.get(task_id)
            if task and task.cancel_event:
                task.cancel_event.set()
                task.is_active = False
                return True
        return False

    def cancel_tasks(self, task_ids: List[str]) -> int:
        """Batch-cancel multiple tasks under a single lock acquisition."""
        count = 0
        with self.graph_lock:
            for task_id in task_ids:
                task = self.task_graph.get(task_id)
                if task and task.cancel_event:
                    task.cancel_event.set()
                    task.is_active = False
                    count += 1
        return count

    def demote_job(self, job_id: str, new_priority: Priority):
        """Demote a running source job to a lower priority.

        Subsequent generator slices will be scheduled at new_priority.
        """
        with self.active_jobs_lock:
            job = self.active_jobs.get(job_id)
        if job:
            job.priority = new_priority
            logger.info(f"Demoted job '{job_id}' to {new_priority.name}.")

    def cancel_job(self, job_id: str):
        """Cancels a source job, preventing any further processing of its items."""
        with self.active_jobs_lock:
            job = self.active_jobs.pop(job_id, None)
        
        if job:
            logger.info(f"RenderManager: Received request to cancel job '{job.job_id}'.")
            job.cancel()
            logger.info(f"Job '{job_id}' cancelled and removed.")
        else:
            logger.warning(f"Job '{job_id}' not found for cancellation.")

    def submit_source_job(self, job: SourceJob):
        """Submits a new source job to the unified task queue as a cooperative task."""
        logger.info(f"RenderManager: Submitted new source job '{job.job_id}'.")
        with self.active_jobs_lock:
            if job.job_id in self.active_jobs:
                logger.warning(f"Job '{job.job_id}' is already active. Ignoring submission.")
                return
            self.active_jobs[job.job_id] = job

        # Create the first wrapper task to kick off the generator
        initial_task_id = f"job_slice::{job.job_id}::0"
        self.submit_task(
            initial_task_id, # Pass task_id positionally
            job.priority,    # Pass priority positionally
            self._cooperative_generator_runner, # Pass func positionally
            job, 0 # Pass job and 0 as positional arguments for *args
        )

    def submit_task(self, task_id: str, priority: Priority, func: Callable, *args,
                    dependencies: Optional[Set[str]] = None,
                    callback: Optional[Callable] = None,
                    task_type: TaskType = TaskType.SIMPLE,
                    on_complete_callback: Optional[Callable] = None,
                    cancel_event: Optional[threading.Event] = None,
                    **kwargs) -> bool:
        """
        Submits a task to the orchestrator. If a task with the same ID exists,
        it can be upgraded to a higher priority. This is handled by invalidating
        the old task and queueing a new one, ensuring the queue order is updated.
        """
        if self._shutting_down.is_set():
            logging.warning(f"RenderManager shutting down. Rejecting task '{task_id}'.")
            return False

        _result = True
        # Callback to fire immediately after lock release (for already-completed/failed tasks).
        # Storing and never firing callbacks for done tasks is a silent failure.
        _done_callback: Optional[Callable] = None
        # When True, skip priority-inheritance and queue logic (task already handled).
        _skip_graph_update = False
        _task_to_enqueue: Optional[RenderTask] = None

        with self.graph_lock:
            task = self.task_graph.get(task_id)

            if task: # Task exists, update it
                # If the task is already running or completed/failed, do not re-submit.
                # However, if it's pending/paused and has a higher priority, update it.
                if task.state in (TaskState.RUNNING, TaskState.COMPLETED, TaskState.FAILED):
                    logger.warning(f"Task '{task_id}' already exists with state {task.state.name}. Ignoring new submission.")
                    if priority > task.priority:
                        logging.warning(f"Task '{task_id}' is {task.state.name}. Cannot re-submit with higher priority {priority.name}.")
                    else:
                        logging.debug(f"Task '{task_id}' is {task.state.name}. Ignoring new submission with lower/equal priority.")
                    if callback:
                        if task.state in (TaskState.COMPLETED, TaskState.FAILED):
                            # Task already done — schedule callback to fire after we release the
                            # lock, rather than silently storing it where it will never fire.
                            _done_callback = callback
                        else: # RUNNING — store for normal dispatch on completion
                            with self.task_callbacks_lock:
                                self.task_callbacks.setdefault(task_id, []).append(callback)
                    callback = None  # prevent re-storage at end of function
                    _result = False
                    _skip_graph_update = True

                # Task is PENDING or PAUSED (queued). Upgrade its priority if the new one is higher.
                elif priority > task.priority:
                    logging.info(f"Upgrading priority for task '{task_id}' from {task.priority.name} to {priority.name}.")
                    # Invalidate the old task. The worker will discard it when it's dequeued.
                    task.is_active = False

                    # Create a new, high-priority task to replace it.
                    new_task = RenderTask(
                        task_id=task.task_id, func=func, priority=priority,
                        args=args, kwargs=kwargs, dependencies=(dependencies or set()).copy(),
                        task_type=task.task_type, on_complete_callback=on_complete_callback,
                        dependents=task.dependents, # Preserve dependents
                        cancel_event=task.cancel_event or cancel_event,  # Preserve cancel_event
                    )
                    self.task_graph[task_id] = new_task
                    task = new_task # Continue with the new task object
                else:
                    # Update args so the queued task runs with the latest values (e.g. latest rating).
                    task.args = args
                    task.kwargs = kwargs
                    logging.debug(f"Task '{task_id}' pending — updated args in-place.")
                    if callback:
                        with self.task_callbacks_lock:
                            self.task_callbacks.setdefault(task_id, []).append(callback)
                    callback = None  # already stored
                    _skip_graph_update = True  # task is already queued, don't re-queue
            else: # New task
                task = RenderTask(
                    task_id=task_id, priority=priority, func=func,
                    args=args, kwargs=kwargs,
                    dependencies=(dependencies or set()).copy(),
                    task_type=task_type,
                    on_complete_callback=on_complete_callback,
                    cancel_event=cancel_event,
                )
                self.task_graph[task_id] = task

                # Link dependencies
                for dep_id in task.dependencies:
                    if dep_id in self.task_graph:
                        self.task_graph[dep_id].dependents.add(task_id)
                    else:
                        # This case should be handled by application logic; a dependency should be submitted first.
                        logging.warning(f"Task '{task_id}' submitted with an unknown dependency '{dep_id}'.")

            if not _skip_graph_update:
                # Priority Inheritance (propagate priority upwards to dependencies)
                tasks_to_visit = deque(task.dependencies)
                visited = set(task.dependencies)
                while tasks_to_visit:
                    dep_id = tasks_to_visit.popleft()
                    if dep_id in self.task_graph:
                        dep_task = self.task_graph[dep_id]
                        if task.priority > dep_task.priority:
                            logging.debug(f"Inheritance: Upgrading '{dep_id}' from {dep_task.priority.name} to {priority.name}.")
                            dep_task.priority = priority
                            for sub_dep_id in dep_task.dependencies:
                                if sub_dep_id not in visited:
                                    tasks_to_visit.append(sub_dep_id)
                                    visited.add(sub_dep_id)

                # If task is runnable (no pending dependencies) and not already queued, mark
                # it as QUEUED inside the lock, then enqueue outside to avoid holding graph_lock
                # while PriorityQueue acquires its internal mutex.
                if not task.dependencies and task.state == TaskState.PENDING:
                    logging.debug(f"Task '{task_id}' is runnable, adding to queue.")
                    task.state = TaskState.QUEUED
                    _task_to_enqueue = task

        # Enqueue outside graph_lock to avoid nested-lock contention.
        if _task_to_enqueue is not None:
            self.task_queue.put(_task_to_enqueue)

        # Fire callback for already-done tasks outside the lock to avoid potential deadlock
        # if the callback itself calls submit_task.
        if _done_callback is not None:
            try:
                _done_callback(task_id, None, None)
            except Exception as e:  # why: callbacks are user-supplied; exceptions must not re-enter the task graph
                logging.error(f"Late callback for already-done task '{task_id}' failed: {e}", exc_info=True)

        if callback:
            with self.task_callbacks_lock:
                self.task_callbacks.setdefault(task_id, []).append(callback)
        return _result

    def update_task_priorities(self, task_ids: Set[str], priority: Priority = Priority.GUI_REQUEST):
        """
        Dynamically upgrades the priority of existing tasks and their dependencies
        by re-submitting them. This correctly leverages the invalidation strategy.
        """
        logger.debug(f"Request to upgrade priority for {len(task_ids)} tasks to {priority.name}.")
        
        tasks_to_resubmit = {}
        with self.graph_lock:
            # First, find all tasks that need upgrading, including dependencies.
            bfs = deque(self.task_graph[tid] for tid in task_ids if tid in self.task_graph)
            visited = set(task_ids)

            while bfs:
                task = bfs.popleft()
                if task.priority < priority:
                    tasks_to_resubmit[task.task_id] = task
                    for dep_id in task.dependencies:
                        if dep_id not in visited and dep_id in self.task_graph:
                            visited.add(dep_id)
                            bfs.append(self.task_graph[dep_id])
        
        # Outside the lock, re-submit the tasks. submit_task handles invalidation.
        for task in tasks_to_resubmit.values():
            self.submit_task(
                task.task_id, priority, task.func, *task.args,
                dependencies=task.dependencies, task_type=task.task_type,
                on_complete_callback=task.on_complete_callback,
                cancel_event=task.cancel_event, **task.kwargs
            )

    def downgrade_task_priorities(self, task_ids: Set[str], priority: Priority):
        """
        Downgrades pending tasks to a lower priority using the same invalidation
        + re-queue strategy as update_task_priorities. RUNNING/COMPLETED/FAILED
        tasks are not touched.
        """
        logger.debug(f"Request to downgrade priority for {len(task_ids)} tasks to {priority.name}.")

        tasks_to_enqueue: List[RenderTask] = []
        with self.graph_lock:
            for tid in task_ids:
                task = self.task_graph.get(tid)
                if (task is None
                        or task.priority <= priority
                        or task.state in (TaskState.RUNNING,
                                          TaskState.COMPLETED,
                                          TaskState.FAILED)):
                    continue

                # Invalidate the old high-priority entry sitting in the queue.
                task.is_active = False

                # Create a replacement at the lower priority.
                new_task = RenderTask(
                    task_id=task.task_id,
                    func=task.func,
                    priority=priority,
                    args=task.args,
                    kwargs=task.kwargs,
                    dependencies=task.dependencies.copy(),
                    task_type=task.task_type,
                    on_complete_callback=task.on_complete_callback,
                    dependents=task.dependents,
                    cancel_event=task.cancel_event,
                )
                self.task_graph[tid] = new_task

                if not new_task.dependencies:
                    new_task.state = TaskState.QUEUED
                    tasks_to_enqueue.append(new_task)

        for t in tasks_to_enqueue:
            self.task_queue.put(t)

    def _emit_scan_complete(self, job: SourceJob, slice_index: int):
        """Emit a scan_complete notification for gui_scan jobs."""
        from network import protocol
        job_parts = job.job_id.split('::', 2)
        session_id = job_parts[1] if len(job_parts) > 2 else None
        job_path = job_parts[2] if len(job_parts) > 2 else job_parts[-1]
        if "gui_scan" in job.job_id:
            completion_data = protocol.ScanCompleteData(path=job_path, file_count=slice_index, files=[])
            notification = protocol.Notification(type="scan_complete", data=completion_data.model_dump(), session_id=session_id)
            try:
                self.notification_queue.put_nowait(notification)
            except Full:
                logging.warning(f"Notification queue full; dropping scan_complete for job '{job.job_id}'.")

    def _cooperative_generator_runner(self, job: SourceJob, slice_index: int):
        logger.debug(f"Executing job slice '{job.job_id}::{slice_index}'.")
        # 1. Immediately check for cancellation.
        if job.is_cancelled():
            logger.info(f"Skipping slice {slice_index} for cancelled job '{job.job_id}'.")
            return
            
        # 3. Process the yielded item.
        logger.debug(f"[{job.job_id}::{slice_index}] Calling next() on generator.")
        item = next(job.generator, None)
        logger.debug(f"[{job.job_id}::{slice_index}] Generator yielded item: {'<batch>' if isinstance(item, list) else item}")

        # 4. If the generator is exhausted, the job is complete.
        if item is None:
            logger.info(
                f"[chunking] generator exhausted for '{job.job_id}' at slice={slice_index}. "
                f"Emitting scan_complete and calling on_complete."
            )
            with self.active_jobs_lock:
                self.active_jobs.pop(job.job_id, None)

            # Emit scan_complete BEFORE on_complete so the GUI receives it
            # before any previews_ready from tasks created by on_complete.
            self._emit_scan_complete(job, slice_index)

            if job.on_complete:
                try:
                    job.on_complete()
                except Exception as e:  # why: on_complete is caller-supplied; exceptions must not abort the generator dispatch loop
                    logging.error(f"on_complete callback for job '{job.job_id}' failed: {e}", exc_info=True)
            return

        # 5. Process the yielded item and create child tasks.
        items_to_process = item if isinstance(item, list) else [item]
        _is_daemon_job = job.job_id.startswith("daemon_idx::")
        job_parts = job.job_id.split('::', 2)
        # Daemon indexing jobs have no GUI session; session_id only applies to gui_scan jobs.
        session_id = None if _is_daemon_job else (job_parts[1] if len(job_parts) > 2 else None)
        job_path = job_parts[2] if len(job_parts) > 2 else job_parts[-1]

        # Suppress scan_progress for daemon indexing and post-scan task-creation
        # jobs — the GUI blindly adds every file from scan_progress to its model,
        # which would pollute the view or duplicate already-known entries.
        _suppress_progress = _is_daemon_job or job.job_id.startswith("post_scan::")
        if not _suppress_progress:
            from network import protocol
            notification_data = protocol.ScanProgressData(
                path=job_path,
                files=[protocol.ImageEntryModel(path=p) for p in items_to_process],
            )
            notification = protocol.Notification(type="scan_progress", data=notification_data.model_dump(), session_id=session_id)
            logger.info(
                f"[chunking] generator_runner: scan_progress for '{job.job_id}' "
                f"slice={slice_index}, files_in_batch={len(items_to_process)}, "
                f"queue_size={self.notification_queue.qsize()}"
            )
            try:
                self.notification_queue.put_nowait(notification)
            except Full:
                logging.warning(f"Notification queue full; dropping scan_progress for job '{job.job_id}'.")

        # Only create backend processing tasks if the job is configured to do so.
        # For fast GUI scans, this will be false.
        if job.create_tasks:
            effective_priority = job.task_priority if job.task_priority is not None else job.priority
            for file_path in items_to_process:
                tasks = job.task_factory(file_path, effective_priority)
                for task in tasks:
                    self.submit_task(
                        task.task_id, task.priority, task.func, *task.args,
                        dependencies=task.dependencies, task_type=task.task_type,
                        on_complete_callback=task.on_complete_callback, **task.kwargs
                    )

        # 6. Schedule the next slice of this job.
        next_slice_index = slice_index + 1
        next_task_id = f"job_slice::{job.job_id}::{next_slice_index}"
        next_priority = job.priority
        logger.info(
            f"[chunking] scheduling next slice: {next_task_id} "
            f"(priority={next_priority}, queue_depth={self.task_queue.qsize()})"
        )
        success = self.submit_task(
            next_task_id,
            next_priority,
            self._cooperative_generator_runner,
            job, next_slice_index
        )
        if not success:
            logger.error(f"Job '{job.job_id}': failed to schedule next slice. Sending scan_complete.")
            with self.active_jobs_lock:
                self.active_jobs.pop(job.job_id, None)
            self._emit_scan_complete(job, slice_index)

    def _on_task_finished(self, task: RenderTask):
        """Handles post-execution logic: unlocking dependents and managing IPC state."""
        dependents_to_enqueue: List[RenderTask] = []
        with self.graph_lock:
            # Unlock dependent tasks; collect runnable ones for enqueueing outside the lock.
            for dependent_id in list(task.dependents):
                dependent_task = self.task_graph.get(dependent_id)
                if dependent_task:
                    dependent_task.dependencies.discard(task.task_id)
                    if not dependent_task.dependencies and dependent_task.state == TaskState.PENDING:
                        logging.debug(f"Task '{task.task_id}' finished, unlocking '{dependent_id}'. Adding to queue.")
                        dependent_task.state = TaskState.QUEUED
                        dependents_to_enqueue.append(dependent_task)

            # Prune completed/failed tasks with no remaining dependents to avoid unbounded growth.
            if not task.dependents:
                self.task_graph.pop(task.task_id, None)
                # Cascade: remove this task from its predecessors' dependent sets.
                # If a predecessor is now a leaf and already done, prune it too.
                for dep_id in task.dependencies:
                    dep_task = self.task_graph.get(dep_id)
                    if dep_task is not None:
                        dep_task.dependents.discard(task.task_id)
                        if not dep_task.dependents and dep_task.state in (TaskState.COMPLETED, TaskState.FAILED):
                            self.task_graph.pop(dep_id, None)

        # Enqueue newly-runnable dependents outside graph_lock.
        for dt in dependents_to_enqueue:
            self.task_queue.put(dt)
        
    def _worker_loop(self, worker_id: int):
        """
        A unified worker that can execute both simple tasks and generators.
        """
        thread_name = threading.current_thread().name
        logging.debug(f"RenderManager: Worker {worker_id} ({thread_name}) started.")
        while self._running:
            task: Optional[RenderTask] = None
            try:
                # Get the next highest priority task. Short timeout to allow shutdown check.
                task = self.task_queue.get(timeout=0.2)
                
                # If a task has been invalidated (e.g., by a priority upgrade), discard it.
                # Do NOT call task_done() here — the finally block handles it unconditionally.
                if not task.is_active:
                    continue
                
                # Check if the task is a shutdown sentinel
                if task.task_id == '_SHUTDOWN_':
                    logging.debug(f"RenderManager: Worker {worker_id} ({thread_name}) received shutdown sentinel. Exiting.")
                    break

                # Mark task as running: set graph state first so other threads
                # see RUNNING before the task appears in active_tasks.
                with self.graph_lock:
                    task.state = TaskState.RUNNING
                task.worker_thread_id = worker_id
                with self.active_tasks_lock:
                    self.active_tasks[worker_id] = task
                
                self._execute_simple_task(task)

            except Empty:
                # No tasks in queue within the timeout, continue waiting/checking shutdown flag
                continue
            except Exception as e:  # why: worker loop guard; unexpected exceptions must not kill the thread
                logging.error(f"RenderManager: Worker {worker_id} ({thread_name}) encountered general error: {e}", exc_info=True)
                # task_done() is called unconditionally in the finally block below.
            finally:
                if task:
                    with self.active_tasks_lock:
                        self.active_tasks.pop(worker_id, None)
                    self.task_queue.task_done() # Indicate task is complete for queue.join()
                    
                    # Emit signal if shutting down (used by main_window to know when shutdown is safe)
                    if self._shutting_down.is_set(): self.taskCompleted.emit()

    def _execute_simple_task(self, task: RenderTask):
        """Executes a standard, short-lived function and its callback on completion."""
        logger.debug(f"Worker executing SIMPLE task '{task.task_id}'.")
        result, error = None, None
        try:
            if task.cancel_event and task.cancel_event.is_set():
                with self.graph_lock:
                    task.state = TaskState.COMPLETED
                return

            result = task.func(*task.args, **task.kwargs)
            with self.graph_lock: task.state = TaskState.COMPLETED
        except Exception as e:  # why: task func is arbitrary user/plugin code; exceptions mark task FAILED without killing the worker
            error = e
            with self.graph_lock: task.state = TaskState.FAILED
            logger.error(f"Task '{task.task_id}' failed: {e}", exc_info=True)
        finally:
            self._on_task_finished(task)
            self._execute_callbacks(task, success=(error is None), result=result, error=error)
            
            # --- EXECUTE THE ON-COMPLETE CALLBACK ---
            if task.on_complete_callback:
                try:
                    logging.debug(f"Executing on_complete_callback for task '{task.task_id}'.")
                    task.on_complete_callback()
                except Exception as e:  # why: on_complete_callback is caller-supplied; must not propagate into the worker
                    logger.error(f"on_complete_callback for '{task.task_id}' failed: {e}", exc_info=True)

    def _execute_callbacks(self, task: RenderTask, success: bool, result: Any = None, error: Optional[Exception] = None):
        """Execute registered callbacks for a completed task."""
        task_id = task.task_id
        if self._shutting_down.is_set():
            logging.debug(f"RenderManager shutting down, skipping callbacks for task '{task_id}'.")
            with self.task_callbacks_lock:
                self.task_callbacks.pop(task_id, None)
            return

        with self.task_callbacks_lock:
            # Retrieve and remove callbacks for this task_id
            callbacks = self.task_callbacks.pop(task_id, [])
        
        for callback in callbacks:
            try:
                if success:
                    callback(task_id, result, None)
                else:
                    callback(task_id, None, error)
            except Exception as e:  # why: callbacks are user-supplied; one failure must not prevent remaining callbacks
                logging.error(f"RenderManager: Callback for task '{task_id}' failed: {e}", exc_info=True)

    def prepare_for_shutdown(self):
        """
        Prepares the manager for a graceful shutdown. Non-blocking.
        Stops accepting new tasks. Currently running tasks will complete.
        """
        logger.info("RenderManager: Preparing for graceful shutdown. No new tasks will be accepted.")
        self._shutting_down.set()
        
    def shutdown(self, timeout: float = 30.0):
        """
        Gracefully shuts down all worker threads. It clears any pending tasks
        from the queue and then waits for currently running tasks to complete.
        This is a blocking operation. Idempotent with respect to prepare_for_shutdown().
        """
        if not self._running and not self.worker_threads:
            logger.warning("RenderManager: Already shut down.")
            return

        logger.info("RenderManager: Initiating shutdown. Discarding pending tasks.")
        self._shutting_down.set()
        
        # Cancel any active jobs to prevent them from rescheduling.
        with self.active_jobs_lock:
            for job_id in list(self.active_jobs.keys()):
                job = self.active_jobs.pop(job_id)
                job.cancel()

        # Clear the queue of any tasks that haven't been started,
        # ensuring only active tasks complete.
        discarded_count = 0
        with self.graph_lock: # Protect task_graph during cleanup
            tasks_to_remove_from_graph = []
            while True:
                try:
                    task = self.task_queue.get_nowait()
                    if task.task_id == '_SHUTDOWN_': # Keep shutdown sentinel for workers
                        self.task_queue.put(task)
                        continue
                    
                    self.task_queue.task_done()
                    if task.state == TaskState.QUEUED: # Only remove tasks that were queued but not running
                        tasks_to_remove_from_graph.append(task.task_id)
                        discarded_count += 1
                except Empty:
                    break
            
            # Remove discarded tasks from the graph
            for task_id in tasks_to_remove_from_graph:
                if task_id in self.task_graph:
                    # Remove self from dependents of dependencies
                    for dep_id in list(self.task_graph[task_id].dependencies):
                        if dep_id in self.task_graph:
                            self.task_graph[dep_id].dependents.discard(task_id)
                    # Remove self from graph
                    del self.task_graph[task_id]

        if discarded_count > 0:
            logger.info(f"RenderManager: Discarded {discarded_count} pending tasks.")

        # Now, wait only for tasks that were already running.
        # Signal workers to exit by adding sentinel to the queue for each worker.
        self._running = False # Signal workers to finish their current task and exit loop
        for _ in range(self.num_workers):
            self.task_queue.put(RenderTask(
                priority=Priority.SHUTDOWN,
                task_id='_SHUTDOWN_',
                func=lambda: None
            ))
        
        # Wait for all workers to finish processing and exit
        all_exited = True
        for i, worker in enumerate(self.worker_threads):
            worker.join(timeout)
            if worker.is_alive():
                logger.warning(f"RenderManager: Worker {i} did not stop gracefully within timeout.")
                all_exited = False

        self.worker_threads.clear()
        # why: task_queue.join() blocks until every dequeued item has a matching
        # task_done().  If a worker timed out above it will never call task_done()
        # for its in-flight item, so join() would hang forever.  Only safe when
        # every worker has exited.
        if all_exited:
            self.task_queue.join()
        
        # Clear any remaining tasks in graph (e.g., dependencies that never got added to queue)
        with self.graph_lock:
            self.task_graph.clear()
            
        logger.info("RenderManager: Shutdown complete.")
        self.shutdownFinished.emit()
