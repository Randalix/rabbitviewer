import os
import importlib.util
import logging
import threading
import time
from typing import Dict, Callable, Any

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)
from scripts.script_api import ScriptAPI
from core.event_system import event_system, EventType


class _MainThreadRelay(QObject):
    """Executes callables on the Qt main thread via a queued signal."""
    _dispatch = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dispatch.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


class Script:
    def __init__(self, name: str, path: str, module):
        self.name = name
        self.path = path
        self.module = module
        self.run_script: Callable = getattr(module, "run_script")

class ScriptManager:
    def __init__(self, main_window):
        self.main_window = main_window
        self.scripts: Dict[str, Script] = {}
        self._relay = _MainThreadRelay(main_window)
        self.api = ScriptAPI(main_window, main_thread_invoke=self._relay._dispatch.emit)
        event_system.subscribe(EventType.RUN_SCRIPT, self._on_run_script_event)

    def _on_run_script_event(self, event_data):
        self.run_script(event_data.script_name)

    def load_scripts(self, scripts_dir: str) -> None:
        """Scripts must have a 'run_script' function."""
        if not os.path.exists(scripts_dir):  # disk-io: scripts directory check
            logger.warning(f"Scripts directory not found: {scripts_dir}")
            return

        for filename in os.listdir(scripts_dir):  # disk-io: script discovery
            if filename.endswith(".py") and filename not in ("__init__.py", "script_manager.py", "script_api.py"):
                script_name = filename[:-3]
                script_path = os.path.join(scripts_dir, filename)

                try:
                    spec = importlib.util.spec_from_file_location(script_name, script_path)
                    if spec is None or spec.loader is None:
                        raise ImportError(f"Could not load spec for {script_name}")

                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    if hasattr(module, "run_script") and callable(module.run_script):
                        self.scripts[script_name] = Script(script_name, script_path, module)
                        logger.info(f"Loaded script: {script_name}")
                    else:
                        logger.warning(f"Script '{script_name}' does not have a callable 'run_script' function.")

                except Exception as e:  # why: user-supplied scripts may have import errors or syntax issues
                    logger.error(f"Failed to load script {script_name} from {script_path}: {e}")

    def run_script(self, script_name: str, *args: Any, **kwargs: Any) -> bool:
        """Runs the script in a background thread so the GUI stays responsive."""
        script = self.scripts.get(script_name)
        if not script:
            logger.warning(f"Script not found: {script_name}")
            return False

        def _run():
            try:
                t0 = time.perf_counter()
                logger.info("Script '%s' started", script_name)
                script.run_script(self.api, *args, **kwargs)
                logger.info("Script '%s' finished in %.3fs", script_name, time.perf_counter() - t0)
            except Exception as e:  # why: user-supplied scripts may raise anything
                logger.error(f"Error executing script '{script_name}': {e}")

        threading.Thread(target=_run, daemon=True,
                         name=f"script-{script_name}").start()
        return True
