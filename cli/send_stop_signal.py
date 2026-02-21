import sys
import os
import logging
import time
import socket

script_dir = os.path.dirname(__file__)
project_root = os.path.abspath(os.path.join(script_dir, '..'))
sys.path.insert(0, project_root)

from network.socket_client import ThumbnailSocketClient
from config.config_manager import ConfigManager

def send_stop_signal():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    config_manager = ConfigManager()

    socket_path = config_manager.get("system.socket_path")

    if not socket_path:
        logging.error("system.socket_path not found in config.")
        return

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
            return
    else:
        logging.info("Daemon is not running (socket file absent).")
        return

    client = ThumbnailSocketClient(socket_path)

    try:
        if client.shutdown_daemon():
            logging.info("Stop signal sent successfully.")

            timeout = 5
            start_time = time.time()
            while os.path.exists(socket_path) and (time.time() - start_time < timeout):
                time.sleep(0.1)  # why: busy-poll avoidance

            if not os.path.exists(socket_path):
                logging.info("Daemon socket removed; daemon exited cleanly.")
            else:
                logging.warning("Daemon socket still present after stop signal; daemon may need more time.")
        else:
            logging.error("Failed to send stop signal; daemon may be unreachable.")
    finally:
        client.shutdown()

if __name__ == "__main__":
    send_stop_signal()
