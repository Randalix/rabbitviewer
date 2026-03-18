"""Registry for compound task operations dispatched by ScriptAPI."""

import logging
import os
import time
from typing import Optional, Dict, List, Any, Callable

logger = logging.getLogger(__name__)


class TaskOperationRegistry:

    def __init__(self, metadata_db, event_system=None):
        self._db = metadata_db
        self._event_system = event_system
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
        result = trash_with_sidecars(file_paths)

        # For files still on disk (trash failed), try hard-delete as fallback.
        still_present = [p for p in file_paths if os.path.exists(p)]  # disk-io: check which files trash failed to remove
        hard_failed: List[str] = []
        for path in still_present:
            try:
                os.remove(path)
                result["succeeded"] += 1
                result["failed"] -= 1
                logger.info("send2trash fallback: hard-deleted %s", os.path.basename(path))
            except OSError as e:
                logger.warning("send2trash fallback: os.remove failed for %s: %s", path, e)
                hard_failed.append(path)

        if hard_failed:
            self._notify_delete_failed(hard_failed)

        result["hard_failed"] = hard_failed
        return result

    def _notify_delete_failed(self, paths: List[str]) -> None:
        if not self._event_system:
            return
        from core.event_system import EventType, StatusMessageEventData
        names = ", ".join(os.path.basename(p) for p in paths[:3])
        if len(paths) > 3:
            names += f" (+{len(paths) - 3} more)"
        self._event_system.emit(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="task_operations",
            timestamp=time.time(),
            message=f"Could not delete: {names}",
            timeout=8000,
        ))

    def _op_remove_records(self, file_paths: List[str]) -> Dict[str, Any]:
        gone = [p for p in file_paths if not os.path.exists(p)]  # disk-io: guard against removing records for files still on disk
        skipped = len(file_paths) - len(gone)
        if skipped:
            logger.warning(
                "remove_records: skipping %d file(s) still present on disk "
                "(trash likely failed): %s",
                skipped, [os.path.basename(p) for p in file_paths if os.path.exists(p)][:5],  # disk-io: log which files were skipped
            )
        success = self._db.remove_records(gone)
        return {"success": success, "count": len(gone), "skipped": skipped}

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
