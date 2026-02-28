import socket
from typing import Optional

MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_BINARY_MESSAGE_SIZE = 100 * 1024 * 1024  # 100 MB — fullres images can be large

# 1-byte type discriminator prepended to response payloads.
FRAME_JSON = b'\x00'
FRAME_BINARY = b'\x01'


def recv_exactly(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly n bytes from sock. Returns None if the connection is closed
    cleanly or a timeout occurs with no data read. Raises ConnectionError if a
    timeout occurs after a partial read (the stream is now corrupted)."""
    data = bytearray()
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
        except socket.timeout:
            if data:
                raise ConnectionError(f"Timeout after reading {len(data)}/{n} bytes")
            return None
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)
