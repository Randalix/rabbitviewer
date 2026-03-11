"""Shared framed-socket helpers for CLI tools.

4-byte big-endian length prefix + UTF-8 JSON.  stdlib only.
"""

import json
import socket

GUI_SOCKET_PATH = "/tmp/rabbitviewer_gui.sock"


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Socket closed before all bytes received")
        data.extend(chunk)
    return bytes(data)


def call(socket_path: str, payload: dict, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        data = json.dumps(payload).encode()
        sock.sendall(len(data).to_bytes(4, "big") + data)
        length = int.from_bytes(_recv_exactly(sock, 4), "big")
        return json.loads(_recv_exactly(sock, length).decode())
