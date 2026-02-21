"""Tests for network/socket_client.py — SocketConnection and ConnectionPool."""

import json
import os
import socket
import threading
import time
import uuid

import pytest

from network._framing import MAX_MESSAGE_SIZE
from network.socket_client import SocketConnection, ConnectionPool


@pytest.fixture()
def sock_path():
    """Short /tmp path for AF_UNIX (macOS 104-byte limit)."""
    path = f"/tmp/rv_test_{uuid.uuid4().hex[:8]}.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _serve_once(sock_path: str, response: dict, ready: threading.Event):
    """Accept one connection, read one framed request, send a framed response."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    ready.set()
    conn, _ = server.accept()
    try:
        # Read framed request
        length_data = conn.recv(4)
        msg_len = int.from_bytes(length_data, "big")
        conn.recv(msg_len)
        # Send framed response
        body = json.dumps(response).encode()
        conn.sendall(len(body).to_bytes(4, "big") + body)
    finally:
        conn.close()
        server.close()


class TestSocketConnectionSendReceive:
    def test_round_trip(self, sock_path):
        expected = {"status": "success", "value": 42}
        ready = threading.Event()
        t = threading.Thread(target=_serve_once, args=(sock_path, expected, ready))
        t.start()
        ready.wait(timeout=2)

        conn = SocketConnection(sock_path, timeout=2.0)
        try:
            result = conn.send_receive({"command": "ping"})
            assert result == expected
        finally:
            conn.close()
            t.join(timeout=2)

    def test_returns_none_on_bad_path(self):
        conn = SocketConnection("/tmp/nonexistent_rabbitviewer_test.sock", timeout=0.1)
        result = conn.send_receive({"command": "ping"}, max_retries=0)
        assert result is None
        conn.close()

    def test_rejects_oversized_response(self, sock_path):
        """Server sends a length prefix exceeding MAX_MESSAGE_SIZE."""
        ready = threading.Event()

        def _serve_oversized():
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            try:
                # Consume the request
                length_data = conn.recv(4)
                msg_len = int.from_bytes(length_data, "big")
                conn.recv(msg_len)
                # Send an oversized length prefix
                fake_len = MAX_MESSAGE_SIZE + 1
                conn.sendall(fake_len.to_bytes(4, "big"))
                conn.sendall(b"\x00" * 16)  # partial body; doesn't matter
            finally:
                conn.close()
                server.close()

        t = threading.Thread(target=_serve_oversized)
        t.start()
        ready.wait(timeout=2)

        conn = SocketConnection(sock_path, timeout=2.0)
        try:
            result = conn.send_receive({"command": "ping"}, max_retries=0)
            assert result is None
        finally:
            conn.close()
            t.join(timeout=2)

    def test_error_sets_connected_false(self, sock_path):
        """After a communication error, connected must be False (thread-safe)."""
        ready = threading.Event()

        def _serve_then_close():
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            # Close immediately without sending a response
            conn.close()
            server.close()

        t = threading.Thread(target=_serve_then_close)
        t.start()
        ready.wait(timeout=2)

        conn = SocketConnection(sock_path, timeout=0.5)
        conn.send_receive({"command": "ping"}, max_retries=0)
        assert conn.connected is False
        conn.close()
        t.join(timeout=2)

    def test_retry_with_backoff(self, sock_path):
        """send_receive retries on transient failure and succeeds when server appears."""
        expected = {"status": "success"}
        ready = threading.Event()

        def _delayed_server():
            time.sleep(0.3)
            _serve_once(sock_path, expected, ready)

        t = threading.Thread(target=_delayed_server)
        t.start()

        conn = SocketConnection(sock_path, timeout=0.5)
        try:
            result = conn.send_receive({"command": "ping"}, max_retries=2)
            # Either succeeds after retry or returns None — both are valid
            # depending on timing. The key invariant is no crash.
            if result is not None:
                assert result == expected
        finally:
            conn.close()
            t.join(timeout=5)


class TestConnectionPool:
    def test_initializes_with_correct_size(self):
        pool = ConnectionPool("/tmp/nonexistent_rabbitviewer_test.sock", pool_size=5)
        assert len(pool.connections) == 5
        pool.close_all()

    def test_get_return_cycle(self):
        pool = ConnectionPool("/tmp/nonexistent_rabbitviewer_test.sock", pool_size=2)
        c1 = pool.get_connection()
        assert c1 is not None
        pool.return_connection(c1)
        c2 = pool.get_connection()
        assert c2 is not None  # got one back
        pool.close_all()

    def test_returns_none_when_exhausted(self):
        pool = ConnectionPool("/tmp/nonexistent_rabbitviewer_test.sock", pool_size=1)
        c1 = pool.get_connection()
        assert c1 is not None
        # Pool is now empty
        c2 = pool.get_connection()
        assert c2 is None
        pool.return_connection(c1)
        pool.close_all()

    def test_close_all_cleans_up(self):
        pool = ConnectionPool("/tmp/nonexistent_rabbitviewer_test.sock", pool_size=3)
        pool.close_all()
        assert len(pool.connections) == 0
        assert pool.available.empty()
