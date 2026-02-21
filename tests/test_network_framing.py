"""Tests for network/_framing.py — recv_exactly() and MAX_MESSAGE_SIZE."""

import socket
import threading
import pytest

from network._framing import recv_exactly, MAX_MESSAGE_SIZE


@pytest.fixture()
def socketpair():
    """Yield a connected AF_UNIX socketpair and close both ends after the test."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    yield a, b
    a.close()
    b.close()


class TestRecvExactly:
    def test_reads_exact_bytes(self, socketpair):
        reader, writer = socketpair
        writer.sendall(b"hello")
        assert recv_exactly(reader, 5) == b"hello"

    def test_accumulates_across_multiple_sends(self, socketpair):
        reader, writer = socketpair
        payload = b"abcdefghij"
        # Send one byte at a time from a thread
        def _send():
            for byte in payload:
                writer.sendall(bytes([byte]))
        t = threading.Thread(target=_send)
        t.start()
        result = recv_exactly(reader, len(payload))
        t.join(timeout=2)
        assert result == payload

    def test_returns_none_on_eof(self, socketpair):
        reader, writer = socketpair
        writer.close()
        assert recv_exactly(reader, 4) is None

    def test_returns_none_on_clean_timeout(self, socketpair):
        reader, writer = socketpair
        reader.settimeout(0.05)
        assert recv_exactly(reader, 4) is None

    def test_raises_on_partial_read_timeout(self, socketpair):
        reader, writer = socketpair
        reader.settimeout(0.05)
        writer.sendall(b"\x00\x00")  # 2 of 4 bytes
        with pytest.raises(ConnectionError, match="Timeout after reading 2/4 bytes"):
            recv_exactly(reader, 4)

    def test_reads_large_payload(self, socketpair):
        reader, writer = socketpair
        payload = b"\xAB" * 100_000
        def _send():
            writer.sendall(payload)
        t = threading.Thread(target=_send)
        t.start()
        result = recv_exactly(reader, len(payload))
        t.join(timeout=5)
        assert result == payload

    def test_zero_bytes_returns_empty(self, socketpair):
        reader, _writer = socketpair
        assert recv_exactly(reader, 0) == b""


class TestMaxMessageSize:
    def test_value(self):
        assert MAX_MESSAGE_SIZE == 10 * 1024 * 1024
