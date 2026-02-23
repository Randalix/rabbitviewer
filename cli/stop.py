"""Send a graceful shutdown signal to the RabbitViewer daemon."""

import os
import logging
import socket
import time

from network.socket_client import ThumbnailSocketClient
from config.config_manager import ConfigManager


def stop_daemon(timeout: float = 5.0) -> bool:
    """Stop the running daemon. Returns True if it shut down cleanly."""
    config_manager = ConfigManager()
    socket_path = config_manager.get("system.socket_path")

    if not socket_path:
        logging.error("system.socket_path not found in config.")
        return False

    logging.info(f"Sending stop signal to daemon at {socket_path}...")

    if os.path.exists(socket_path):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as temp_sock:
                temp_sock.settimeout(0.5)
                temp_sock.connect(socket_path)
        except (socket.error, ConnectionRefusedError, socket.timeout):
            logging.info("Daemon not active (connection failed or timed out).")
            logging.info(f"Stale socket file at {socket_path}; attempting cleanup.")
            try:
                os.remove(socket_path)
                logging.info("Stale socket file removed.")
            except OSError as e:
                logging.warning(f"Could not remove stale socket file: {e}")
            return True
    else:
        logging.info("Daemon is not running (socket file absent).")
        return True

    client = ThumbnailSocketClient(socket_path)

    try:
        if client.shutdown_daemon():
            logging.info("Stop signal sent successfully.")

            start_time = time.time()
            while os.path.exists(socket_path) and (time.time() - start_time < timeout):
                time.sleep(0.1)

            if not os.path.exists(socket_path):
                logging.info("Daemon socket removed; daemon exited cleanly.")
                return True
            else:
                logging.warning("Daemon socket still present after stop signal; daemon may need more time.")
                return False
        else:
            logging.error("Failed to send stop signal; daemon may be unreachable.")
            return False
    finally:
        client.shutdown()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    stop_daemon()


if __name__ == "__main__":
    main()
