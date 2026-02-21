import socket
from typing import Optional


def recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly n bytes from sock. Returns None if the connection is closed."""
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)
