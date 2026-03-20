# 002 — AI background indexing freezes and causes severe lag

**Status:** fixed
**Files:** `core/ai_task_coordinator.py`, `core/onnx_runtime.py`, `main.py`

## Observed Behaviour

The application would either freeze completely or experience periods of extreme unresponsiveness (lasting 30+ seconds) shortly after startup, particularly when opening a directory with new files. Logs indicated this coincided with the creation of ONNX runtime sessions for CoreML.

An initial attempt to fix this by limiting concurrency with blocking semaphores exacerbated the freezes, as it led to deadlocks in the `RenderManager` worker pool.

## Root Cause Analysis

A deep analysis revealed three distinct but interacting root causes:

1.  **Concurrent CoreML Initialization:** The `StartupScheduler` in `main.py` was initiating parallel pre-warming for different AI model families (CLIP, Face Detection, etc.). Each pre-warming process would start in a separate thread and attempt to create an ONNX `InferenceSession` with the `CoreMLExecutionProvider`. The CoreML framework is not thread-safe on first initialization, and these concurrent calls would corrupt its internal state, leading to a hard application freeze.

2.  **Task Submission Race Condition:** The startup sequence would begin directory scanning and submitting AI-powered jobs (`auto_orient_job`, `clip_index_job`, etc.) *before* the model pre-warming threads had a chance to complete. This created a race where a `RenderManager` worker would pick up an AI task, attempt to get the required model, and trigger a cache miss in `onnx_runtime.get_session()`. This caused the worker thread itself to block for ~30 seconds on model compilation, starving the worker pool and preventing any higher-priority UI tasks from running. The `auto-orient-v1` model was completely missing from the pre-warming list, making this race condition guaranteed for that task type.

3.  **Ineffective Concurrency Limiting:** The original attempt to prevent CPU saturation from parallel AI inference used a simple `threading.Semaphore(1)` around the inference call inside the task function. This was fundamentally flawed, as it caused all but one worker to block on the semaphore, exhausting the worker pool and preventing them from doing other work. This was the cause of the severe lag, as opposed to the freezes which were caused by the CoreML race condition.

## The Fix: Throttled, Gated Job Submission

The final solution is a new scheduling architecture implemented primarily in `core/ai_task_coordinator.py` that addresses all three root causes:

1.  **Sequential Pre-warming:** All AI model pre-warming has been consolidated into a single `prewarm_all_models` function which is kicked off by a single startup task in `main.py`. This function loads every required AI model (CLIP, Face, and Orientation) in sequence within a single background thread, completely eliminating the CoreML initialization race condition.

2.  **Gated Job Submission:** A `threading.Event` is now associated with each model family (e.g., `_clip_models_ready`). The `SourceJob` for each AI feature now uses a `_gated_generator`. This generator blocks at the very beginning, waiting for the appropriate event to be set by the pre-warming thread. This guarantees that no AI tasks are even created or submitted to the `RenderManager` until their required models are safely loaded and cached.

3.  **Throttled Task Creation:** To solve the worker pool exhaustion and lag, the concept of limiting concurrency was moved from *execution* to *submission*. The generators for heavy AI jobs (CLIP, Face) are now "throttled" using a new `_throttled_gated_generator`.
    - This generator uses a `threading.Semaphore` (e.g., with a value of 2) to limit how many tasks can be "in flight" at once.
    - It `acquires` the semaphore *before* yielding a file path to be turned into a task. If the limit (e.g., 2) is reached, the generator thread itself blocks until a slot is free. This is safe as it does not block any of the `RenderManager` workers.
    - The task function itself (`_generate_clip_embedding`, `_face_detect_task`) now `releases` the semaphore in a `finally` block upon completion.
    - This creates a robust feedback loop that keeps the number of active, CPU-intensive AI tasks low without blocking the worker pool, ensuring the UI remains responsive.

## Notes

- **Unavoidable First-Run Lag:** The one-time compilation of models by CoreML is an extremely CPU-intensive process. While it now happens safely in the background, it can still cause noticeable system-wide sluggishness for its duration (~30-60 seconds) on the very first application run after installation or a model update. This is a fundamental limitation of the underlying libraries.
- A minor, unrelated bug causing invalid tasks like `face_detect::/` was also fixed by adding guards to the task creation methods.