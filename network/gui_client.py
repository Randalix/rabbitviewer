"""
Thin CLI client for the GuiServer Unix socket.

Pure stdlib — no Qt dependency.
Framing: 4-byte big-endian length prefix + UTF-8 JSON body.
"""

import json
import socket
from typing import List, Optional


class GuiClient:
    """One-shot client for interacting with a running RabbitViewer GUI."""

    def __init__(self, socket_path: str, timeout: float = 5.0):
        self._socket_path = socket_path
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_selection(self) -> List[str]:
        """Return the list of currently selected image paths."""
        response = self._send({"command": "get_selection"})
        if response and response.get("status") == "success":
            return response.get("paths", [])
        return []

    def remove_images(self, paths: List[str]) -> bool:
        """Ask the GUI to remove *paths* from the current view."""
        response = self._send({"command": "remove_images", "paths": paths})
        return response is not None and response.get("status") == "success"

    def clear_selection(self) -> bool:
        """Clear the current selection in the GUI."""
        response = self._send({"command": "clear_selection"})
        return response is not None and response.get("status") == "success"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, payload: dict) -> Optional[dict]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(self._socket_path)

                data = json.dumps(payload).encode()
                sock.sendall(len(data).to_bytes(4, byteorder="big") + data)

                length_data = self._recv_exactly(sock, 4)
                if not length_data:
                    return None
                message_length = int.from_bytes(length_data, byteorder="big")
                message_data = self._recv_exactly(sock, message_length)
                if not message_data:
                    return None
                return json.loads(message_data.decode())
        except Exception as e:  # why: any socket or protocol error from the GUI server should degrade gracefully
            print(f"GuiClient error: {e}")
            return None

    @staticmethod
    def _recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
        data = bytearray()
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except socket.timeout:
                return None
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)
