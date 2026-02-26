"""Tests for the run_tasks compound task dispatch pipeline."""
import os
import time
import threading
import uuid

import pytest

from core.rendermanager import RenderManager, Priority
from core.metadata_database import MetadataDatabase
from network import protocol


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return result


class MockConfigManager:
    def __init__(self, overrides=None):
        self._cfg = {
            "thumbnail_size": 128,
            "min_file_size": 0,
            "ignore_patterns": [],
            "cache_dir": None,
            "watch_paths": [],
        }
        if overrides:
            self._cfg.update(overrides)

    def get(self, key, default=None):
        keys = key.split(".")
        val = self._cfg
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default


@pytest.fixture()
def rm():
    manager = RenderManager(num_workers=2)
    manager.start()
    yield manager
    manager.shutdown(timeout=5)


@pytest.fixture()
def tm(tmp_path, rm):
    """ThumbnailManager wired to a live RenderManager."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    config = MockConfigManager({"cache_dir": str(cache_dir)})
    db = MetadataDatabase(str(tmp_path / "test.db"))

    from core.thumbnail_manager import ThumbnailManager
    manager = ThumbnailManager(config, db, num_workers=2)
    # Replace with our controlled RenderManager
    manager.render_manager.shutdown(timeout=2)
    manager.render_manager = rm
    yield manager


# ===========================================================================
#  Protocol Models
# ===========================================================================

class TestProtocolModels:
    def test_task_operation_fields(self):
        op = protocol.TaskOperation(name="send2trash", file_paths=["/a.jpg", "/b.jpg"])
        assert op.name == "send2trash"
        assert op.file_paths == ["/a.jpg", "/b.jpg"]

    def test_run_tasks_request_fields(self):
        op = protocol.TaskOperation(name="send2trash", file_paths=["/a.jpg"])
        req = protocol.RunTasksRequest(operations=[op])
        assert req.command == "run_tasks"
        assert len(req.operations) == 1

    def test_run_tasks_response(self):
        resp = protocol.RunTasksResponse(
            task_id="script_task::1",
            queued_count=2,
        )
        assert resp.status == "success"
        assert resp.task_id == "script_task::1"
        assert resp.queued_count == 2


# ===========================================================================
#  ThumbnailManager Operation Registry
# ===========================================================================

class TestOperationRegistry:
    def test_registry_contains_default_operations(self, tm):
        assert tm.get_task_operation("send2trash") is not None
        assert tm.get_task_operation("remove_records") is not None

    def test_unknown_operation_returns_none(self, tm):
        assert tm.get_task_operation("nonexistent") is None

    def test_op_remove_records(self, tm, tmp_path):
        db = tm.metadata_db
        # Create a real file so set_thumbnail_paths accepts it
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8fake")
        img_str = str(img)
        db.set_thumbnail_paths(img_str, thumbnail_path="/cache/thumb.jpg")

        result = tm._op_remove_records([img_str])
        assert result["success"] is True
        assert result["count"] == 1

    def test_op_remove_records_empty(self, tm):
        result = tm._op_remove_records([])
        assert result["success"] is True

    def test_op_send2trash_missing_file(self, tm):
        result = tm._op_send2trash(["/nonexistent/file.jpg"])
        assert result["failed"] == 1
        assert result["succeeded"] == 0


# ===========================================================================
#  execute_compound_task
# ===========================================================================

class TestExecuteCompoundTask:
    def test_executes_operations_in_order(self, tm):
        call_log = []

        def op_a(paths):
            call_log.append(("a", paths))
            return {"ok": True}

        def op_b(paths):
            call_log.append(("b", paths))
            return {"ok": True}

        tm._task_operations["op_a"] = op_a
        tm._task_operations["op_b"] = op_b

        results = tm.execute_compound_task([
            ("op_a", ["/x.jpg"]),
            ("op_b", ["/y.jpg"]),
        ])

        assert call_log == [("a", ["/x.jpg"]), ("b", ["/y.jpg"])]
        assert results["op_a"] == {"ok": True}
        assert results["op_b"] == {"ok": True}

    def test_unknown_operation_logged_in_results(self, tm):
        results = tm.execute_compound_task([("bogus", ["/x.jpg"])])
        assert "error" in results["bogus"]

    def test_operation_exception_captured(self, tm):
        def op_crash(paths):
            raise RuntimeError("boom")

        tm._task_operations["crasher"] = op_crash
        results = tm.execute_compound_task([("crasher", ["/x.jpg"])])
        assert "boom" in results["crasher"]["error"]

    def test_continues_after_failure(self, tm):
        call_log = []

        def op_fail(paths):
            raise RuntimeError("fail")

        def op_ok(paths):
            call_log.append("ok")
            return {"done": True}

        tm._task_operations["op_fail"] = op_fail
        tm._task_operations["op_ok"] = op_ok

        results = tm.execute_compound_task([
            ("op_fail", ["/x.jpg"]),
            ("op_ok", ["/y.jpg"]),
        ])
        assert "error" in results["op_fail"]
        assert results["op_ok"] == {"done": True}
        assert call_log == ["ok"]


# ===========================================================================
#  Compound Task via RenderManager
# ===========================================================================

class TestCompoundTaskAsync:
    def test_compound_task_runs_in_worker(self, tm, rm):
        results_holder = {}
        event = threading.Event()

        def capture_op(paths):
            results_holder["paths"] = paths
            results_holder["thread"] = threading.current_thread().name
            event.set()
            return {"captured": True}

        tm._task_operations["capture"] = capture_op

        rm.submit_task(
            "test_compound::1",
            Priority.NORMAL,
            tm.execute_compound_task,
            [("capture", ["/a.jpg", "/b.jpg"])],
        )

        assert event.wait(timeout=5), "compound task did not execute"
        assert results_holder["paths"] == ["/a.jpg", "/b.jpg"]
        assert results_holder["thread"] != threading.current_thread().name


# ===========================================================================
#  Dispatch Handler (socket_thumbnailer)
# ===========================================================================

class TestRunTasksDispatch:
    """Tests _handle_run_tasks via a minimal ThumbnailSocketServer."""

    @pytest.fixture()
    def server(self, tm):
        """Minimal socket server with a short /tmp path for macOS."""
        sock_path = f"/tmp/rv_test_{uuid.uuid4().hex[:8]}.sock"

        from network.socket_thumbnailer import ThumbnailSocketServer
        server = ThumbnailSocketServer(sock_path, tm)
        yield server
        server.running = False
        try:
            server.server_socket.close()
        except Exception:
            pass
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

    def _make_request(self, operations):
        """Build a run_tasks request dict with pre-constructed TaskOperation objects."""
        ops = [protocol.TaskOperation(name=n, file_paths=[protocol.ImageEntryModel(path=fp) for fp in p]) for n, p in operations]
        return {"command": "run_tasks", "operations": ops}

    def test_dispatch_run_tasks_success(self, server):
        server.thumbnail_manager._task_operations["noop"] = lambda paths: {"ok": True}
        req_data = self._make_request([("noop", ["/a.jpg"])])
        response = server._handle_run_tasks(req_data)
        assert response.status == "success"
        assert response.queued_count == 1
        assert response.task_id.startswith("script_task::")

    def test_dispatch_run_tasks_empty_operations(self, server):
        req_data = self._make_request([])
        response = server._handle_run_tasks(req_data)
        assert response.status == "error"

    def test_dispatch_run_tasks_unknown_operation(self, server):
        req_data = self._make_request([("does_not_exist", ["/a.jpg"])])
        response = server._handle_run_tasks(req_data)
        assert response.status == "error"
        assert "does_not_exist" in response.message

    def test_dispatch_table_routes_to_handler(self, server):
        assert "run_tasks" in server._command_handlers
        assert server._command_handlers["run_tasks"] == server._handle_run_tasks

    def test_dispatch_command_unknown(self, server):
        response = server._dispatch_command("nonexistent_command", {})
        assert response.status == "error"
        assert "Unknown command" in response.message

    def test_task_id_increments(self, server):
        server.thumbnail_manager._task_operations["noop"] = lambda paths: {}
        req_data = self._make_request([("noop", [])])
        r1 = server._handle_run_tasks(req_data)
        r2 = server._handle_run_tasks(req_data)
        id1 = int(r1.task_id.split("::")[-1])
        id2 = int(r2.task_id.split("::")[-1])
        assert id2 == id1 + 1


# ===========================================================================
#  MetadataDatabase.remove_records optimisation
# ===========================================================================

class TestRemoveRecordsOptimisation:
    def test_remove_records_cleans_cache_files(self, tmp_env):
        db = tmp_env["db"]
        cache_dir = tmp_env["cache_dir"]

        # Create a real image file and a cache file
        img_path = tmp_env["tmp_path"] / "real.jpg"
        img_path.write_bytes(b"\xff\xd8fake")
        cache_file = cache_dir / "thumb.jpg"
        cache_file.write_bytes(b"fake")

        img = str(img_path)
        db.set_thumbnail_paths(img, thumbnail_path=str(cache_file))

        result = db.remove_records([img])
        assert result is True
        assert not cache_file.exists()

    def test_remove_records_handles_missing_cache(self, tmp_env):
        """Cache files that don't exist should not cause errors."""
        db = tmp_env["db"]
        img_path = tmp_env["tmp_path"] / "real2.jpg"
        img_path.write_bytes(b"\xff\xd8fake")
        img = str(img_path)
        db.set_thumbnail_paths(img, thumbnail_path="/nonexistent/thumb.jpg")

        result = db.remove_records([img])
        assert result is True

    def test_remove_records_idempotent(self, tmp_env):
        """Calling remove_records twice should not error."""
        db = tmp_env["db"]
        img_path = tmp_env["tmp_path"] / "real3.jpg"
        img_path.write_bytes(b"\xff\xd8fake")
        img = str(img_path)
        db.set_thumbnail_paths(img, thumbnail_path="/cache/thumb.jpg")

        assert db.remove_records([img]) is True
        assert db.remove_records([img]) is True

    def test_remove_records_empty_list(self, tmp_env):
        db = tmp_env["db"]
        assert db.remove_records([]) is True
