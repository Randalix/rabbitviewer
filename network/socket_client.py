from __future__ import annotations
import socket
import json
import logging
import os
import threading
import queue
import time
import uuid
from typing import List, Optional, TYPE_CHECKING
_ValidationErrors = (ValueError, TypeError, KeyError)
from ._framing import recv_exactly, MAX_MESSAGE_SIZE, MAX_BINARY_MESSAGE_SIZE, FRAME_JSON, FRAME_BINARY
if TYPE_CHECKING:
    from . import protocol as protocol

# Lazy singleton so public methods can reference `protocol` without
# repeating `from . import protocol` in every function body.
_protocol_module = None

def _lazy_protocol():
    global _protocol_module
    if _protocol_module is None:
        from . import protocol
        _protocol_module = protocol
    return _protocol_module

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

    def _send_and_recv(self, data: dict):
        """Send JSON request and receive raw response frame. Returns (frame_type, payload)."""
        message = json.dumps(data).encode()
        length_prefix = len(message).to_bytes(4, byteorder='big')
        self.sock.sendall(length_prefix + message)

        length_data = self._recv_exactly(4)
        if not length_data:
            raise ConnectionError("Failed to read message length")

        message_length = int.from_bytes(length_data, byteorder='big')
        if message_length > MAX_BINARY_MESSAGE_SIZE:
            raise ConnectionError(f"Message too large: {message_length} bytes")

        message_data = self._recv_exactly(message_length)
        if not message_data:
            raise ConnectionError("Failed to read complete message")

        # Peek at the 1-byte type discriminator.
        frame_type = message_data[0:1]
        payload = message_data[1:]
        return frame_type, payload

    def send_receive(self, data: dict, max_retries: int = 2) -> Optional[dict]:
        retries = 0
        while retries <= max_retries:
            try:
                if not self.ensure_connected():
                    retries += 1
                    time.sleep(0.1 * (2 ** retries))  # Exponential backoff: 0.2s, 0.4s
                    continue

                with self.lock:
                    frame_type, payload = self._send_and_recv(data)

                    if frame_type == FRAME_BINARY:
                        raise ConnectionError("Unexpected binary response on JSON channel")
                    return json.loads(payload.decode())

            except (ConnectionError, socket.error) as e:
                logging.debug(f"Communication error (attempt {retries + 1}): {e}")
                with self.lock:
                    self.connected = False
                retries += 1
                if retries <= max_retries:
                    time.sleep(0.1 * (2 ** retries))  # Exponential backoff: 0.2s, 0.4s

        logging.error("Failed to communicate after retries")
        return None

    def send_receive_binary(self, data: dict, max_retries: int = 2):
        """Send JSON request, receive response that may be JSON dict or raw bytes."""
        retries = 0
        while retries <= max_retries:
            try:
                if not self.ensure_connected():
                    retries += 1
                    time.sleep(0.1 * (2 ** retries))
                    continue

                with self.lock:
                    frame_type, payload = self._send_and_recv(data)

                    if frame_type == FRAME_BINARY:
                        return payload  # Raw image bytes.
                    return json.loads(payload.decode())  # JSON dict.

            except (ConnectionError, socket.error) as e:
                logging.debug(f"Communication error (attempt {retries + 1}): {e}")
                with self.lock:
                    self.connected = False
                retries += 1
                if retries <= max_retries:
                    time.sleep(0.1 * (2 ** retries))

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
        self.available.put(conn)

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
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.connection_pool = ConnectionPool(socket_path)
        self.session_id = str(uuid.uuid4())

    @staticmethod
    def _to_entry_models(paths: List[str]):
        """Convert a list of string paths to ImageEntryModel objects."""
        protocol = _lazy_protocol()
        return [protocol.ImageEntryModel(path=p) for p in paths]

    def _send_request(self, request: protocol.Request, response_model: type[protocol.Response]) -> Optional[protocol.Response]:
        """Send a request using a connection from the pool and validate the response."""
        protocol = _lazy_protocol()
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

        except _ValidationErrors as e:
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
        protocol = _lazy_protocol()
        request = protocol.GetDirectoryFilesRequest(path=path, recursive=recursive)
        return self._send_request(request, protocol.GetDirectoryFilesResponse)

    def request_previews(self, image_paths: List[str], priority: int = 50) -> Optional[protocol.RequestPreviewsResponse]:
        """Asynchronously requests the generation of previews for a list of images."""
        protocol = _lazy_protocol()
        logging.debug(f"SocketClient: Requesting previews for {len(image_paths)} paths with priority {priority}.")
        request = protocol.RequestPreviewsRequest(image_paths=self._to_entry_models(image_paths), priority=priority)
        return self._send_request(request, protocol.RequestPreviewsResponse)

    def update_viewport_heatmap(
        self,
        upgrade_pairs: List[tuple],
        paths_to_downgrade: List[str],
        fullres_pairs: List[tuple],
        fullres_to_cancel: List[str],
    ) -> Optional[protocol.RequestPreviewsResponse]:
        """Sends heatmap-based viewport update with per-path priorities."""
        protocol = _lazy_protocol()
        request = protocol.UpdateViewportRequest(
            paths_to_upgrade=[
                protocol.PathPriority(entry=protocol.ImageEntryModel(path=p), priority=pri) for p, pri in upgrade_pairs
            ],
            paths_to_downgrade=self._to_entry_models(paths_to_downgrade),
            fullres_to_request=[
                protocol.PathPriority(entry=protocol.ImageEntryModel(path=p), priority=pri) for p, pri in fullres_pairs
            ],
            fullres_to_cancel=self._to_entry_models(fullres_to_cancel),
        )
        return self._send_request(request, protocol.RequestPreviewsResponse)

    def request_view_image(self, image_path: str):
        """Requests view image generation at FULLRES_REQUEST priority.

        Returns:
            bytes — raw image bytes if the view image was mem-cached on the daemon.
            RequestViewImageResponse — with view_image_path (disk) or None (queued).
            None — on communication failure.
        """
        protocol = _lazy_protocol()
        request = protocol.RequestViewImageRequest(image_entry=protocol.ImageEntryModel(path=image_path))
        request.session_id = self.session_id
        conn = self.connection_pool.get_connection()
        if not conn:
            return None
        try:
            result = conn.send_receive_binary(request.model_dump())
            if result is None:
                return None
            if isinstance(result, bytes):
                return result  # Raw image bytes from daemon mem cache.
            # JSON dict — validate as usual.
            if result.get("status") == "error":
                return protocol.ErrorResponse.model_validate(result)
            return protocol.RequestViewImageResponse.model_validate(result)
        except _ValidationErrors as e:
            logging.error(f"Client-side validation error for request_view_image: {e}")
            return protocol.ErrorResponse(message=str(e))
        except Exception as e:  # why: socket and protocol errors from external daemon
            logging.error(f"request_view_image failed: {e}")
            conn.close()
            return None
        finally:
            self.connection_pool.return_connection(conn)

    def get_previews_status(self, image_paths: List[str]) -> Optional[protocol.GetPreviewsStatusResponse]:
        protocol = _lazy_protocol()
        request = protocol.GetPreviewsStatusRequest(image_paths=self._to_entry_models(image_paths))
        return self._send_request(request, protocol.GetPreviewsStatusResponse)

    def set_rating(self, image_paths: List[str], rating: int) -> Optional[protocol.Response]:
        protocol = _lazy_protocol()
        request = protocol.SetRatingRequest(image_paths=self._to_entry_models(image_paths), rating=rating)
        return self._send_request(request, protocol.Response)

    def get_metadata_batch(self, image_paths: List[str], priority: bool = False) -> Optional[protocol.GetMetadataBatchResponse]:
        protocol = _lazy_protocol()
        request = protocol.GetMetadataBatchRequest(image_paths=self._to_entry_models(image_paths), priority=priority)
        return self._send_request(request, protocol.GetMetadataBatchResponse)

    def get_filtered_file_paths(self, text_filter: str, star_states: List[bool],
                               tag_names: Optional[List[str]] = None) -> Optional[protocol.GetFilteredFilePathsResponse]:
        """Ask the daemon to return a filtered set of file paths."""
        protocol = _lazy_protocol()
        request = protocol.GetFilteredFilePathsRequest(
            text_filter=text_filter,
            star_states=star_states,
            tag_names=tag_names or [],
        )
        return self._send_request(request, protocol.GetFilteredFilePathsResponse)

    def set_tags(self, image_paths: List[str], tags: List[str]) -> Optional[protocol.Response]:
        protocol = _lazy_protocol()
        request = protocol.SetTagsRequest(image_paths=self._to_entry_models(image_paths), tags=tags)
        return self._send_request(request, protocol.Response)

    def remove_tags(self, image_paths: List[str], tags: List[str]) -> Optional[protocol.Response]:
        protocol = _lazy_protocol()
        request = protocol.RemoveTagsRequest(image_paths=self._to_entry_models(image_paths), tags=tags)
        return self._send_request(request, protocol.Response)

    def get_tags(self, directory_path: str = "") -> Optional[protocol.GetTagsResponse]:
        """Fetches all tags, with directory-scoped tags separated for autocomplete."""
        protocol = _lazy_protocol()
        request = protocol.GetTagsRequest(directory_path=directory_path)
        return self._send_request(request, protocol.GetTagsResponse)

    def get_image_tags(self, image_paths: List[str]) -> Optional[protocol.GetImageTagsResponse]:
        protocol = _lazy_protocol()
        request = protocol.GetImageTagsRequest(image_paths=self._to_entry_models(image_paths))
        return self._send_request(request, protocol.GetImageTagsResponse)

    def move_records(self, moves: List[protocol.MoveRecord]) -> Optional[protocol.MoveRecordsResponse]:
        """Tell the daemon to update file_path entries for a batch of moved files."""
        protocol = _lazy_protocol()
        request = protocol.MoveRecordsRequest(moves=moves)
        return self._send_request(request, protocol.MoveRecordsResponse)

    def run_tasks(self, operations: List[protocol.TaskOperation]) -> Optional[protocol.RunTasksResponse]:
        """Submit compound task operations to the daemon for async execution."""
        protocol = _lazy_protocol()
        request = protocol.RunTasksRequest(operations=operations)
        return self._send_request(request, protocol.RunTasksResponse)

    def comfyui_generate(self, image_path: str, prompt: str, denoise: float = 0.30,
                         workflow: str = "") -> Optional[protocol.ComfyUIGenerateResponse]:
        """Request ComfyUI generation for an image."""
        protocol = _lazy_protocol()
        request = protocol.ComfyUIGenerateRequest(
            image_entry=protocol.ImageEntryModel(path=image_path),
            prompt=prompt,
            denoise=denoise,
            workflow=workflow,
        )
        return self._send_request(request, protocol.ComfyUIGenerateResponse)

    # --- Daemon Control Methods ---
    def is_socket_file_present(self) -> bool:
        return os.path.exists(self.socket_path)

    def _send_simple_command(self, command: str) -> bool:
        """Helper for simple, non-Pydantic commands."""
        protocol = _lazy_protocol()
        conn = self.connection_pool.get_connection()
        if not conn: return False
        try:
            request = protocol.Request(command=command, session_id=self.session_id)
            response = conn.send_receive(request.model_dump())
            return response is not None and response.get("status") == "success"
        finally:
            self.connection_pool.return_connection(conn)

    def shutdown_daemon(self) -> bool:
        return self._send_simple_command("shutdown")

    def shutdown(self):
        self.connection_pool.close_all()
