import socket
import json
import logging
import threading
import time
from core.event_system import EventSystem, EventType, DaemonNotificationEventData
from ._framing import recv_exactly, MAX_MESSAGE_SIZE

class NotificationListener(threading.Thread):
    def __init__(self, socket_path: str, event_system: EventSystem):
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.event_system = event_system
        self._stop_event = threading.Event()


    def run(self):
        retry_delay = 1.0  # Start at 1s, grow to max 30s
        while not self._stop_event.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    # why: intentional blocking recv in daemon thread; process exit terminates the thread
                    sock.connect(self.socket_path)
                    logging.info("Notification client connected to daemon.")
                    retry_delay = 1.0  # Reset on successful connection

                    handshake = json.dumps({"type": "register_notifier"}).encode()
                    length_prefix = len(handshake).to_bytes(4, byteorder='big')
                    sock.sendall(length_prefix + handshake)

                    sock.settimeout(2.0)
                    while not self._stop_event.is_set():
                        try:
                            length_data = recv_exactly(sock, 4)
                        except ConnectionError:
                            logging.warning("Notification daemon: partial read on length prefix.")
                            break
                        if not length_data:
                            # recv_exactly returns None on both clean timeout and peer close.
                            # Use a non-blocking peek to distinguish them.
                            try:
                                sock.setblocking(False)
                                peek = sock.recv(1, socket.MSG_PEEK)
                                sock.settimeout(2.0)
                                if not peek:
                                    logging.warning("Notification daemon disconnected.")
                                    break
                                # Data available — fall through to re-read
                                continue
                            except BlockingIOError:
                                # Nothing available — normal idle timeout
                                sock.settimeout(2.0)
                                continue
                            except OSError:
                                logging.warning("Notification daemon disconnected.")
                                break

                        message_length = int.from_bytes(length_data, byteorder='big')
                        if message_length > MAX_MESSAGE_SIZE:
                            logging.error(f"Notification message too large: {message_length} bytes")
                            break

                        try:
                            message_data = recv_exactly(sock, message_length)
                        except ConnectionError:
                            logging.warning("Notification daemon: partial read on message body.")
                            break
                        if not message_data:
                            logging.warning("Notification daemon disconnected while reading message body.")
                            break

                        try:
                            notification = json.loads(message_data.decode())
                            logging.debug(f"Received notification: {notification}")

                            if not isinstance(notification, dict):
                                logging.error(f"Unexpected notification type {type(notification)!r}; skipping.")
                                continue

                            event = DaemonNotificationEventData(
                                    event_type=EventType.DAEMON_NOTIFICATION,
                                    source=self.__class__.__name__,
                                    timestamp=time.time(),
                                    notification_type=notification.get("type", ""),
                                    data=notification.get("data", {})
                                )
                            self.event_system.publish(event)
                        except json.JSONDecodeError:
                            logging.error(f"Failed to decode notification JSON. Raw data: {message_data!r}")

            except (ConnectionRefusedError, FileNotFoundError):
                logging.debug(f"Could not connect to notification server. Retrying in {retry_delay:.0f}s.")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)
            except Exception as e:  # why: any socket or deserialization error in the listener loop must not kill the thread
                logging.error(f"Error in notification listener: {e}", exc_info=True)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    def stop(self):
        self._stop_event.set()
