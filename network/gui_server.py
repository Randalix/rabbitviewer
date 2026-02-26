"""
GUI-side Unix socket server that exposes selection state and accepts mutation
commands from CLI tools.

Socket path: /tmp/rabbitviewer_gui.sock
Framing: 4-byte big-endian length prefix + UTF-8 JSON body (same as daemon).
"""

import json
import logging
import os
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal, Qt

from . import protocol
from ._framing import recv_exactly, MAX_MESSAGE_SIZE

GUI_SOCKET_PATH = "/tmp/rabbitviewer_gui.sock"


class GuiServer(QObject):
    """Listens on a Unix socket in a background thread and dispatches commands
    to the Qt main thread via a signal bridge."""

    _dispatch_to_main = Signal(object)

    def __init__(self, main_window):
        super().__init__()
        self._main_window = main_window
        self._running = False
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

        self._dispatch_to_main.connect(self._execute_on_main, Qt.QueuedConnection)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        try:
            os.remove(GUI_SOCKET_PATH)
        except FileNotFoundError:
            pass

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(GUI_SOCKET_PATH)
        self._server_socket.listen(5)
        self._running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="GuiServerThread")
        self._thread.start()
        logging.info(f"GuiServer listening on {GUI_SOCKET_PATH}")

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        try:
            os.remove(GUI_SOCKET_PATH)
        except FileNotFoundError:
            pass
        logging.info("GuiServer stopped.")

    # ------------------------------------------------------------------
    # Internal — main-thread bridge
    # ------------------------------------------------------------------

    def _execute_on_main(self, func):
        func()

    def _run_on_main_sync(self, func) -> None:
        """Post *func* to the Qt main thread and block until it completes."""
        done = threading.Event()

        def wrapped():
            try:
                func()
            finally:
                done.set()

        self._dispatch_to_main.emit(wrapped)
        done.wait(timeout=5.0)

    # ------------------------------------------------------------------
    # Internal — socket listener
    # ------------------------------------------------------------------

    def _accept_loop(self):
        self._server_socket.settimeout(1.0)
        while self._running:
            try:
                conn, _ = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn: socket.socket):
        try:
            conn.settimeout(10.0)
            length_data = recv_exactly(conn, 4)
            if not length_data:
                return
            message_length = int.from_bytes(length_data, byteorder="big")
            if message_length > MAX_MESSAGE_SIZE:
                raise ConnectionError(f"Message too large: {message_length} bytes")
            message_data = recv_exactly(conn, message_length)
            if not message_data:
                return

            request_data = json.loads(message_data.decode())
            response = self._dispatch_command(request_data)

            payload = response.encode()
            conn.sendall(len(payload).to_bytes(4, byteorder="big") + payload)
        except Exception as e:  # why: any unhandled error in client handler must not crash the server thread
            logging.error(f"GuiServer: error handling client: {e}", exc_info=True)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _dispatch_command(self, request_data: dict) -> str:
        command = request_data.get("command", "")
        try:
            if command == "get_selection":
                return self._cmd_get_selection()
            elif command == "remove_images":
                req = protocol.RemoveImagesRequest.model_validate(request_data)
                return self._cmd_remove_images([e.path for e in req.paths])
            elif command == "clear_selection":
                return self._cmd_clear_selection()
            else:
                return protocol.GuiErrorResponse(message=f"Unknown command: {command}").model_dump_json()
        except Exception as e:  # why: dispatch errors from unknown commands or buggy handlers
            logging.error(f"GuiServer: error dispatching '{command}': {e}", exc_info=True)
            return protocol.GuiErrorResponse(message=str(e)).model_dump_json()

    def _cmd_get_selection(self) -> str:
        mw = self._main_window
        selected_paths = list(mw.selection_state.selected_paths)  # why: CPython GIL guarantees atomic set snapshot; not safe under free-threaded builds
        return protocol.GetSelectionResponse(
            paths=[protocol.ImageEntryModel(path=p) for p in sorted(selected_paths)]
        ).model_dump_json()

    def _cmd_remove_images(self, paths: list) -> str:
        self._run_on_main_sync(lambda: self._main_window.remove_images(paths))
        return protocol.GuiSuccessResponse().model_dump_json()

    def _cmd_clear_selection(self) -> str:
        from core.selection import ReplaceSelectionCommand  # why: avoids circular import at module load

        def _do():
            cmd = ReplaceSelectionCommand(
                paths=set(),
                source="gui_server",
                timestamp=time.time(),
            )
            self._main_window.selection_processor.process_command(cmd)

        self._run_on_main_sync(_do)
        return protocol.GuiSuccessResponse().model_dump_json()
