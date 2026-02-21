"""Tests for network/gui_client.py — GuiClient._recv_exactly static method."""

import socket
import threading
import pytest

from network.gui_client import GuiClient


@pytest.fixture()
def socketpair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    yield a, b
    a.close()
    b.close()


class TestGuiClientRecvExactly:
    def test_full_read(self, socketpair):
        reader, writer = socketpair
        writer.sendall(b"abcd")
        assert GuiClient._recv_exactly(reader, 4) == b"abcd"

    def test_returns_none_on_close(self, socketpair):
        reader, writer = socketpair
        writer.close()
        assert GuiClient._recv_exactly(reader, 4) is None

    def test_raises_on_partial_read_timeout(self, socketpair):
        reader, writer = socketpair
        reader.settimeout(0.05)
        writer.sendall(b"\x01\x02")  # 2 of 4 bytes
        with pytest.raises(ConnectionError, match="Timeout after reading 2/4 bytes"):
            GuiClient._recv_exactly(reader, 4)

    def test_returns_none_on_clean_timeout(self, socketpair):
        reader, writer = socketpair
        reader.settimeout(0.05)
        assert GuiClient._recv_exactly(reader, 4) is None
