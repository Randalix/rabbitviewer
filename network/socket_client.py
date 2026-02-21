import socket
import json
import logging
import os
import threading
import queue
import time
import uuid
from typing import List, Optional
from pydantic import ValidationError
from . import protocol
from ._framing import recv_exactly

class SocketConnection:
    """Represents a single socket connection with retry logic"""
    def __init__(self, socket_path: str, timeout: float = 20.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()
        self.connected = False

    def ensure_connected(self) -> bool:
        with self.lock:
            if self.connected and self.sock:
                return True
            return self._connect()

    def _connect(self) -> bool:
        try:
            if self.sock:
                self.sock.close()
            
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.socket_path)
            self.connected = True
            return True
        except Exception as e:  # why: connection errors include ConnectionRefusedError, FileNotFoundError, OSError — all expected on daemon absence
            logging.debug(f"Connection failed: {e}")
            self.connected = False
            return False

    def send_receive(self, data: dict, max_retries: int = 2) -> Optional[dict]:
        retries = 0
        while retries <= max_retries:
            try:
                if not self.ensure_connected():
                    retries += 1
                    time.sleep(0.1 * (2 ** retries))  # Exponential backoff: 0.2s, 0.4s
                    continue

                with self.lock:
                    message = json.dumps(data).encode()
                    length_prefix = len(message).to_bytes(4, byteorder='big')
                    self.sock.sendall(length_prefix + message)

                    length_data = self._recv_exactly(4)
                    if not length_data:
                        raise ConnectionError("Failed to read message length")

                    message_length = int.from_bytes(length_data, byteorder='big')

                    message_data = self._recv_exactly(message_length)
                    if not message_data:
                        raise ConnectionError("Failed to read complete message")

                    return json.loads(message_data.decode())

            except (ConnectionError, socket.error) as e:
                logging.debug(f"Communication error (attempt {retries + 1}): {e}")
                self.connected = False
                retries += 1
                if retries <= max_retries:
                    time.sleep(0.1 * (2 ** retries))  # Exponential backoff: 0.2s, 0.4s

        logging.error("Failed to communicate after retries")
        return None

    def _recv_exactly(self, n: int) -> Optional[bytes]:
        return recv_exactly(self.sock, n)

    def close(self):
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            self.connected = False

class ConnectionPool:
    """Manages a pool of socket connections"""
    def __init__(self, socket_path: str, pool_size: int = 3):
        self.socket_path = socket_path
        self.pool_size = pool_size
        self.connections: List[SocketConnection] = []
        self.available = queue.Queue()
        self.lock = threading.Lock()
        self._initialize_pool()

    def _initialize_pool(self):
        with self.lock:
            for _ in range(self.pool_size):
                conn = SocketConnection(self.socket_path)
                self.connections.append(conn)
                self.available.put(conn)

    def get_connection(self) -> Optional[SocketConnection]:
        try:
            return self.available.get(timeout=1.0)
        except queue.Empty:
            return None

    def return_connection(self, conn: SocketConnection):
        try:
            self.available.put(conn)
        except queue.Full:
            pass

    def close_all(self):
        with self.lock:
            for conn in self.connections:
                conn.close()
            self.connections.clear()
            while not self.available.empty():
                try:
                    self.available.get_nowait()
                except queue.Empty:
                    break

class ThumbnailSocketClient:
    """Client for interacting with the thumbnail generation service"""
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.connection_pool = ConnectionPool(socket_path)
        self.session_id = str(uuid.uuid4())
        self.lock = threading.Lock()

    def _send_request(self, request: protocol.Request, response_model: type[protocol.Response]) -> Optional[protocol.Response]:
        """Send a request using a connection from the pool and validate the response."""
        conn = self.connection_pool.get_connection()
        if not conn:
            return None

        try:
            request.session_id = self.session_id
            response_dict = conn.send_receive(request.model_dump())
            if response_dict is None:
                return None
            
            if response_dict.get("status") == "error":
                return protocol.ErrorResponse.model_validate(response_dict)
            return response_model.model_validate(response_dict)

        except ValidationError as e:
            logging.error(f"Client-side validation error for command '{request.command}': {e}")
            return protocol.ErrorResponse(message=str(e))
        except Exception as e:  # why: socket and protocol errors from external daemon are untyped
            logging.error(f"Request failed for command '{request.command}': {e}")
            conn.close()  # Mark connection as bad; finally will return it to the pool
            return None
        finally:
            self.connection_pool.return_connection(conn)
                
    def get_directory_files(self, path: str, recursive: bool = True) -> Optional[protocol.GetDirectoryFilesResponse]:
        """Ask the daemon for the definitive list of files in a directory from its database."""
        request = protocol.GetDirectoryFilesRequest(path=path, recursive=recursive)
        return self._send_request(request, protocol.GetDirectoryFilesResponse)

    def request_previews(self, image_paths: List[str], priority: int = 50) -> Optional[protocol.RequestPreviewsResponse]:
        """Asynchronously requests the generation of previews for a list of images."""
        logging.debug(f"SocketClient: Requesting previews for {len(image_paths)} paths with priority {priority}.")
        request = protocol.RequestPreviewsRequest(image_paths=image_paths, priority=priority)
        return self._send_request(request, protocol.RequestPreviewsResponse)

    def update_viewport(self, paths_to_upgrade: List[str],
                        paths_to_downgrade: List[str]) -> Optional[protocol.RequestPreviewsResponse]:
        """Upgrades visible thumbnails to GUI_REQUEST and downgrades scrolled-away ones."""
        request = protocol.UpdateViewportRequest(
            paths_to_upgrade=paths_to_upgrade,
            paths_to_downgrade=paths_to_downgrade,
        )
        return self._send_request(request, protocol.RequestPreviewsResponse)

    def request_view_image(self, image_path: str) -> Optional[protocol.RequestViewImageResponse]:
        """Requests view image generation at FULLRES_REQUEST priority.

        Returns the response immediately. `response.view_image_path` is set when
        the view image was already cached; None means generation has been queued."""
        request = protocol.RequestViewImageRequest(image_path=image_path)
        return self._send_request(request, protocol.RequestViewImageResponse)

    def get_previews_status(self, image_paths: List[str]) -> Optional[protocol.GetPreviewsStatusResponse]:
        """Checks the generation status for a list of image paths."""
        request = protocol.GetPreviewsStatusRequest(image_paths=image_paths)
        return self._send_request(request, protocol.GetPreviewsStatusResponse)

    def set_rating(self, image_paths: List[str], rating: int) -> Optional[protocol.Response]:
        """Sets the star rating for a list of images."""
        request = protocol.SetRatingRequest(image_paths=image_paths, rating=rating)
        return self._send_request(request, protocol.Response)

    def get_metadata_batch(self, image_paths: List[str], priority: bool = False) -> Optional[protocol.GetMetadataBatchResponse]:
        """Retrieves all known metadata for a list of images."""
        request = protocol.GetMetadataBatchRequest(image_paths=image_paths, priority=priority)
        return self._send_request(request, protocol.GetMetadataBatchResponse)

    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool]) -> Optional[protocol.GetFilteredFilePathsResponse]:
        """Ask the daemon to return a filtered set of file paths."""
        request = protocol.GetFilteredFilePathsRequest(
            text_filter=text_filter, 
            star_states=star_states
        )
        return self._send_request(request, protocol.GetFilteredFilePathsResponse)

    def move_records(self, moves: List[protocol.MoveRecord]) -> Optional[protocol.MoveRecordsResponse]:
        """Tell the daemon to update file_path entries for a batch of moved files."""
        request = protocol.MoveRecordsRequest(moves=moves)
        return self._send_request(request, protocol.MoveRecordsResponse)

    # --- Daemon Control Methods ---
    def is_socket_file_present(self) -> bool:
        """Check if the thumbnailer server socket file exists."""
        return os.path.exists(self.socket_path)

    def _send_simple_command(self, command: str) -> bool:
        """Helper for simple, non-Pydantic commands."""
        conn = self.connection_pool.get_connection()
        if not conn: return False
        try:
            request = protocol.Request(command=command, session_id=self.session_id)
            response = conn.send_receive(request.model_dump())
            return response is not None and response.get("status") == "success"
        finally:
            self.connection_pool.return_connection(conn)

    def shutdown_daemon(self) -> bool:
        """Send a command to shut down the daemon."""
        return self._send_simple_command("shutdown")

    def shutdown(self):
        """Clean up resources"""
        self.connection_pool.close_all()
