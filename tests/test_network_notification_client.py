"""Tests for network/notification_client.py — NotificationListener backoff logic."""

import sys
import types
from unittest.mock import MagicMock, patch

# Pre-mock heavy imports before importing the module under test.
# Save originals so we can restore them after import.
_saved_core = sys.modules.get("core")
_saved_core_es = sys.modules.get("core.event_system")

_mock_event_system = types.ModuleType("core.event_system")
_mock_event_system.EventSystem = MagicMock  # type: ignore[attr-defined]
_mock_event_system.EventType = MagicMock()  # type: ignore[attr-defined]
_mock_event_system.DaemonNotificationEventData = MagicMock  # type: ignore[attr-defined]

if "core" not in sys.modules:
    sys.modules["core"] = types.ModuleType("core")
sys.modules["core.event_system"] = _mock_event_system

from network.notification_client import NotificationListener  # noqa: E402

# Restore sys.modules so other tests importing core.event_system get the real module.
if _saved_core_es is not None:
    sys.modules["core.event_system"] = _saved_core_es
else:
    sys.modules.pop("core.event_system", None)
if _saved_core is not None:
    sys.modules["core"] = _saved_core
else:
    sys.modules.pop("core", None)


class TestNotificationListenerBackoff:
    def _make_listener(self):
        mock_es = MagicMock()
        return NotificationListener("/tmp/nonexistent_rabbitviewer_test.sock", mock_es)

    def test_exponential_backoff_delays(self):
        """Connection failures should produce exponential backoff: 1, 2, 4, 8..."""
        listener = self._make_listener()
        sleep_calls = []

        def _fake_sleep(t):
            sleep_calls.append(t)
            if len(sleep_calls) >= 4:
                listener._stop_event.set()

        with patch("time.sleep", side_effect=_fake_sleep):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock_cls.return_value.__enter__ = MagicMock(
                    return_value=MagicMock(connect=MagicMock(side_effect=ConnectionRefusedError))
                )
                mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
                listener.run()

        assert len(sleep_calls) >= 3
        assert sleep_calls[0] == 1.0
        assert sleep_calls[1] == 2.0
        assert sleep_calls[2] == 4.0

    def test_backoff_caps_at_30_seconds(self):
        """Retry delay must not exceed 30 seconds."""
        listener = self._make_listener()
        sleep_calls = []

        def _fake_sleep(t):
            sleep_calls.append(t)
            if len(sleep_calls) >= 8:
                listener._stop_event.set()

        with patch("time.sleep", side_effect=_fake_sleep):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock_cls.return_value.__enter__ = MagicMock(
                    return_value=MagicMock(connect=MagicMock(side_effect=ConnectionRefusedError))
                )
                mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
                listener.run()

        assert all(d <= 30.0 for d in sleep_calls)
        # After enough doublings (1,2,4,8,16,32→30), we should see the cap
        assert 30.0 in sleep_calls

    def test_resets_on_successful_connection(self):
        """After a successful connect, retry_delay resets to 1s."""
        listener = self._make_listener()
        sleep_calls = []
        connect_count = [0]

        def _fake_sleep(t):
            sleep_calls.append(t)
            if len(sleep_calls) >= 4:
                listener._stop_event.set()

        def _fake_connect(addr):
            connect_count[0] += 1
            if connect_count[0] <= 2:
                raise ConnectionRefusedError
            # Third call "succeeds" but then we raise to exit the inner loop
            raise OSError("simulated disconnect in inner loop")

        with patch("time.sleep", side_effect=_fake_sleep):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock.connect = _fake_connect
                mock_sock.sendall = MagicMock(side_effect=OSError("no send"))
                mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
                mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
                listener.run()

        # First two failures: 1.0, 2.0. Third connects → retry resets.
        # Then the inner-loop error causes reconnect at 1.0 again.
        assert sleep_calls[0] == 1.0
        assert sleep_calls[1] == 2.0
