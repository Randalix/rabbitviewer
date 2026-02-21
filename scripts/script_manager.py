import os
import importlib.util
import logging
from typing import Dict, Callable, Any
from scripts.script_api import ScriptAPI

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
        self.api = ScriptAPI(main_window)
        
    def load_scripts(self, scripts_dir: str) -> None:
        """Scripts must have a 'run_script' function."""
        if not os.path.exists(scripts_dir):
            logging.warning(f"Scripts directory not found: {scripts_dir}")
            return

        for filename in os.listdir(scripts_dir):
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
                        logging.info(f"Loaded script: {script_name}")
                    else:
                        logging.warning(f"Script '{script_name}' does not have a callable 'run_script' function.")

                except Exception as e:  # why: user-supplied scripts may have import errors or syntax issues
                    logging.error(f"Failed to load script {script_name} from {script_path}: {e}")

    def run_script(self, script_name: str, *args: Any, **kwargs: Any) -> bool:
        """Returns True if found and run, False if not found or if execution raised."""
        script = self.scripts.get(script_name)
        if script:
            try:
                script.run_script(self.api, *args, **kwargs)
                logging.debug(f"Executed script: {script_name}")
                return True
            except Exception as e:  # why: user-supplied scripts may raise anything
                logging.error(f"Error executing script '{script_name}': {e}")
                return False
        else:
            logging.warning(f"Script not found: {script_name}")
            return False
