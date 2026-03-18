"""Registry for compound task operations dispatched by ScriptAPI."""

import logging
from typing import Optional, Dict, List, Any, Callable

logger = logging.getLogger(__name__)


class TaskOperationRegistry:
    """Maps operation names to handler functions for daemon task dispatch."""

    def __init__(self, metadata_db):
        self._db = metadata_db
        self._operations: Dict[str, Callable] = {
            "send2trash": self._op_send2trash,
            "remove_records": self._op_remove_records,
            "bookmark_copy": self._op_bookmark_copy,
            "bookmark_move": self._op_bookmark_move,
        }

    def get(self, name: str) -> Optional[Callable]:
        return self._operations.get(name)

    def register(self, name: str, handler: Callable) -> None:
        self._operations[name] = handler

    def execute_compound(self, operations: list) -> Dict[str, Any]:
        """Execute a sequence of named operations. Runs in a RenderManager worker thread.

        Each element is ``(name, file_paths)`` or ``(name, file_paths, kwargs)``.
        """
        results: Dict[str, Any] = {}
        for op in operations:
            name, file_paths = op[0], op[1]
            kwargs = op[2] if len(op) > 2 else {}
            handler = self._operations.get(name)
            if not handler:
                logger.error(f"Unknown task operation: {name}")
                results[name] = {"error": f"unknown operation: {name}"}
                continue
            try:
                results[name] = handler(file_paths, **kwargs)
            except Exception as e:  # why: task operations are user-registered handlers; any exception must not crash the worker loop
                logger.error(f"Task operation '{name}' failed: {e}", exc_info=True)
                results[name] = {"error": str(e)}
        return results

    def _op_send2trash(self, file_paths: List[str]) -> Dict[str, Any]:
        from core.file_ops import trash_with_sidecars
        return trash_with_sidecars(file_paths)

    def _op_remove_records(self, file_paths: List[str]) -> Dict[str, Any]:
        success = self._db.remove_records(file_paths)
        return {"success": success, "count": len(file_paths)}

    def _op_bookmark_copy(self, file_paths: List[str], *,
                          dest_dir: str) -> Dict[str, Any]:
        from core.bookmark_manager import execute_bookmark_transfer
        return execute_bookmark_transfer(file_paths, dest_dir, move=False,
                                         db=self._db)

    def _op_bookmark_move(self, file_paths: List[str], *,
                          dest_dir: str) -> Dict[str, Any]:
        from core.bookmark_manager import execute_bookmark_transfer
        return execute_bookmark_transfer(file_paths, dest_dir, move=True,
                                         db=self._db)
