import os
import socket
import json
import logging
from typing import Optional, Dict, List, Tuple, Set, Generator, Any
import threading
import time
from filewatcher.watcher import WatchdogHandler
import sys
from core.directory_scanner import DirectoryScanner
from core.rendermanager import Priority, TaskType, SourceJob
from . import protocol
from ._framing import MAX_MESSAGE_SIZE
from pydantic import ValidationError
import queue # Import for queue.Empty

class ThumbnailSocketServer:
    """Server that handles thumbnail generation and rating requests via Unix domain socket."""

    def __init__(self, socket_path: str, thumbnail_manager, watchdog_handler: Optional[WatchdogHandler] = None):
        """
        Initialize the thumbnail socket server.
        """
        self.socket_path = socket_path
        self.thumbnail_manager = thumbnail_manager
        self.watchdog_handler = watchdog_handler
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.running = True
        self.client_threads = []
        self.is_daemon_mode = False
        self.notification_clients = []
        self.notification_lock = threading.Lock()
        self.active_gui_session_id: Optional[str] = None
        self.session_lock = threading.Lock()
        self._fast_scan_cancel: Optional[threading.Event] = None

        self.directory_scanner = DirectoryScanner(thumbnail_manager, thumbnail_manager.config_manager)

        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(5)
        logging.info(f"Socket bound at {self.socket_path}")

        self.thumbnail_manager.set_socket_server(self)

        # Start a thread to listen for notifications FROM the RenderManager
        self._notification_listener_thread = threading.Thread(target=self._rm_notification_listener_loop, daemon=True)
        self._notification_listener_thread.start()

    def _rm_notification_listener_loop(self):
        """Listens to the RenderManager's output queue and forwards to the GUI."""
        logging.info("RenderManager notification listener started.")
        rm_queue = self.thumbnail_manager.render_manager.notification_queue
        while self.running:
            notification = None
            try:
                notification = rm_queue.get(timeout=1)

                # Filter notifications based on the active session ID.
                with self.session_lock:
                    active_session = self.active_gui_session_id
                if notification.session_id and notification.session_id != active_session:
                    logging.debug(f"Dropping stale notification for session {notification.session_id[:8]}...")
                    continue

                logging.debug(f"SocketServer received notification from RenderManager: {notification.type}")
                self.send_notification(notification)
            except queue.Empty:
                continue
            except Exception as e:  # why: queue or deserialization errors from RenderManager are untyped
                logging.error(f"Error in RenderManager notification listener: {e}", exc_info=True)
                time.sleep(1) # Prevent busy-loop on persistent errors
            finally:
                # task_done() must be called exactly once per get(), regardless of
                # whether the notification was forwarded, filtered, or caused an error.
                if notification is not None:
                    rm_queue.task_done()

    def _run_fast_scan_thread(self, session_id: str, path: str, recursive: bool, cancel: threading.Event):
        """
        Dedicated thread for fast directory discovery. Iterates scan_incremental
        and pushes scan_progress notifications directly to the notification queue,
        bypassing the worker pool entirely so all workers stay free for thumbnails.
        """
        rm = self.thumbnail_manager.render_manager
        for batch in self.directory_scanner.scan_incremental(path, recursive):
            with self.session_lock:
                current_session = self.active_gui_session_id
            if cancel.is_set() or current_session != session_id:
                logging.debug(f"Fast scan cancelled for session {session_id[:8]}")
                return
            notification = protocol.Notification(
                type="scan_progress",
                data=protocol.ScanProgressData(path=path, files=batch).model_dump(),
                session_id=session_id,
            )
            try:
                rm.notification_queue.put(notification, timeout=1)
            except queue.Full:
                logging.warning("Notification queue full; dropping fast scan batch for %s", path)
        logging.info("Fast scan thread complete for %s", path)

    def run_forever(self):
        """Accept and handle connections indefinitely."""
        self.is_daemon_mode = True
        try:
            logging.info(f"Thumbnailer accepting connections on {self.socket_path}")

            # Start the non-blocking, chunked background cleanup of the database.
            self.thumbnail_manager.start_chunked_db_cleanup()

            while self.running:
                try:
                    conn, _ = self.server_socket.accept()
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(conn,),
                        daemon=True
                    )
                    self.client_threads.append(client_thread)
                    client_thread.start()
                except Exception as e:  # why: OS errors on accept (e.g. EBADF during shutdown)
                    if self.running:
                        logging.error(f"Error accepting connection: {e}")
                        time.sleep(0.1)  # Avoid busy-loop if the socket is in a bad state
        except Exception as e:
            logging.error(f"Socket error: {e}")
        finally:
            self.shutdown()

    def handle_client(self, conn: socket.socket):
        """
        Handle a client connection.

        Args:
            conn: Connected socket object for client communication
        """
        try:
            conn.settimeout(10)  # 10 second timeout
            while self.running:
                try:
                    length_data = self._recv_exactly(conn, 4)
                    if not length_data:
                        break

                    message_length = int.from_bytes(length_data, byteorder='big')
                    if message_length > MAX_MESSAGE_SIZE:
                        raise ConnectionError(f"Message too large: {message_length} bytes")

                    message_data = self._recv_exactly(conn, message_length)
                    if not message_data:
                        break

                    request_data = json.loads(message_data.decode())

                    if request_data.get("type") == "register_notifier":
                        with self.notification_lock:
                            if conn not in self.notification_clients:
                                self.notification_clients.append(conn)
                        logging.info("Registered a new notification client.")
                        # Remove the timeout so recv() blocks indefinitely until the
                        # client closes its end of the connection (returns b'').
                        # Without this, the 10 s timeout set above would fire, causing
                        # _recv_exactly to return None and the client to be deregistered.
                        conn.settimeout(None)
                        conn.recv(1024)
                        # Once the client disconnects, break the loop to trigger the finally block for cleanup.
                        break

                    command = request_data.get("command", "unknown")
                    logging.debug(f"Server received command: '{command}'")
                    response = self.handle_request(request_data)

                    logging.debug(f"Server prepared response for '{command}', sending...")
                    response_data = response.encode()
                    length_prefix = len(response_data).to_bytes(4, byteorder='big')
                    conn.sendall(length_prefix + response_data)
                    logging.debug(f"Server successfully sent response for '{command}'.")

                except socket.timeout:
                    continue
                except Exception as e:  # why: socket I/O or framing errors during client message handling
                    logging.error(f"Error handling client: {e}")
                    break
        finally:
            # Clean up from notification list if present
            with self.notification_lock:
                if conn in self.notification_clients:
                    self.notification_clients.remove(conn)
                    logging.info("Unregistered a notification client.")

                    # If a GUI client disconnects, cancel all jobs for its session.
                    with self.session_lock:
                        active_session = self.active_gui_session_id
                    if active_session:
                        render_manager = self.thumbnail_manager.render_manager
                        jobs_to_cancel = [
                            job_id for job_id in render_manager.get_all_job_ids()
                            if active_session in job_id
                        ]
                        for job_id in jobs_to_cancel:
                            logging.info(f"Client for session {active_session[:8]} disconnected. Cancelling job: {job_id}")
                            render_manager.cancel_job(job_id)

                        # Clear the active session, as the GUI is gone.
                        with self.session_lock:
                            self.active_gui_session_id = None

            conn.close()

    def _recv_exactly(self, conn: socket.socket, n: int) -> Optional[bytes]:
        """
        Receive exactly n bytes from the connection.

        Args:
            conn: Socket connection to read from
            n: Number of bytes to read

        Returns:
            Bytes object containing exactly n bytes, or None if connection closed
        """
        data = bytearray()
        while len(data) < n:
            try:
                packet = conn.recv(n - len(data))
                if not packet:
                    return None
                data.extend(packet)
            except socket.timeout:
                if data:
                    raise ConnectionError(f"Timeout after reading {len(data)}/{n} bytes")
                return None
        return bytes(data)

    def handle_request(self, request_data: dict) -> str:
        """
        Handle incoming socket requests by validating against the protocol.
        """
        # Removed the old breakpoint block here
        
        try:
            command = request_data.get("command")
            if not command:
                return protocol.ErrorResponse(message="Request missing 'command' field.").model_dump_json()

            # --- Command Dispatcher ---
            response_model = self._dispatch_command(command, request_data)
            return response_model.model_dump_json()

        except ValidationError as e:
            return protocol.ErrorResponse(message=f"Validation Error: {e}").model_dump_json()
        except Exception as e:  # why: any unhandled error from handler dispatch must not crash the server
            logging.error(f"Error processing request: {e}", exc_info=True)
            return protocol.ErrorResponse(message=f"Internal Server Error: {str(e)}").model_dump_json()
    
    def _dispatch_command(self, command: str, request_data: dict) -> protocol.Response:
        """Dispatches commands to the appropriate handler."""
        with self.session_lock:
            session_id_snapshot = self.active_gui_session_id
        if command == "request_previews":
            req = protocol.RequestPreviewsRequest.model_validate(request_data)
            logging.info(f"SocketServer: Received request_previews for {len(req.image_paths)} paths with priority {req.priority}.")
            success_count = 0
            priority_level = Priority(req.priority)

            # Directly call the priority upgrade logic instead of queueing a task to do it.
            for path in req.image_paths:
                if self.thumbnail_manager.request_thumbnail(
                        path, priority_level, session_id_snapshot):
                    success_count += 1

            return protocol.RequestPreviewsResponse(count=success_count)

        elif command == "get_previews_status":
            req = protocol.GetPreviewsStatusRequest.model_validate(request_data)
            statuses = {}
            for path in req.image_paths:
                # This must be a fast, non-blocking check.
                is_thumbnail_ready = False
                thumbnail_path = None
                view_image_ready = False
                view_image_path = None

                cached_paths = self.thumbnail_manager.metadata_db.get_thumbnail_paths(path)
                if cached_paths:
                    thumbnail_path = cached_paths.get('thumbnail_path')
                    view_image_path = cached_paths.get('view_image_path')
                    if view_image_path and os.path.exists(view_image_path):
                        view_image_ready = True
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        is_thumbnail_ready = True

                statuses[path] = protocol.PreviewStatus(
                    thumbnail_ready=is_thumbnail_ready,
                    thumbnail_path=thumbnail_path if is_thumbnail_ready else None,
                    view_image_ready=view_image_ready,
                    view_image_path=view_image_path if view_image_ready else None
                )
            return protocol.GetPreviewsStatusResponse(statuses=statuses)

        elif command == "get_metadata_batch":
            req = protocol.GetMetadataBatchRequest.model_validate(request_data)

            if req.priority:
                self.thumbnail_manager.render_manager.submit_task(
                    f"metadata_batch::{hash(tuple(req.image_paths))}", # Unique ID for batch
                    Priority.GUI_REQUEST,
                    self.thumbnail_manager.request_metadata_extraction,
                    req.image_paths, Priority.GUI_REQUEST,
                    task_type=TaskType.SIMPLE
                )

            metadata_results = {}
            for path in req.image_paths:
                # Fetch all metadata from DB
                metadata = self.thumbnail_manager.metadata_db.get_metadata(path)
                metadata_results[path] = metadata if metadata else {} # Ensure it's a dict

            return protocol.GetMetadataBatchResponse(metadata=metadata_results)

        elif command == "set_rating":
            req = protocol.SetRatingRequest.model_validate(request_data)

            success_db, _count = self.thumbnail_manager.metadata_db.batch_set_ratings(req.image_paths, req.rating)

            if success_db:
                for path in req.image_paths:
                    self.thumbnail_manager.render_manager.submit_task(
                        f"write_rating::{path}",
                        Priority.NORMAL,
                        self.thumbnail_manager._write_rating_to_file,
                        path, req.rating,
                        task_type=TaskType.SIMPLE
                    )
                return protocol.Response(message="Ratings updated and queued for file write.")
            else:
                return protocol.ErrorResponse(message="Failed to update rating in database.")

        elif command == "shutdown":
            self.shutdown()
            return protocol.Response(message="Server shutting down")

        elif command == "update_viewport":
            req = protocol.UpdateViewportRequest.model_validate(request_data)
            logging.info(
                f"SocketServer: update_viewport — upgrading {len(req.paths_to_upgrade)}, "
                f"downgrading {len(req.paths_to_downgrade)} tasks."
            )
            success_count = 0
            for path in req.paths_to_upgrade:
                if self.thumbnail_manager.request_thumbnail(
                        path, Priority.GUI_REQUEST, session_id_snapshot):
                    success_count += 1

            if req.paths_to_downgrade:
                self.thumbnail_manager.downgrade_thumbnail_tasks(
                    req.paths_to_downgrade, Priority.GUI_REQUEST_LOW
                )
            return protocol.RequestPreviewsResponse(count=success_count)

        elif command == "request_view_image":
            req = protocol.RequestViewImageRequest.model_validate(request_data)
            logging.info(f"SocketServer: Received request_view_image for {req.image_path}")
            view_image_path = self.thumbnail_manager.request_view_image(
                req.image_path, session_id_snapshot
            )
            return protocol.RequestViewImageResponse(view_image_path=view_image_path)

        elif command == "get_filtered_file_paths":
            req = protocol.GetFilteredFilePathsRequest.model_validate(request_data)

            # This is a simplification. A truly robust implementation would wait for metadata
            # extraction to complete. For now, we proceed with what's in the DB.
            visible_paths = self.thumbnail_manager.metadata_db.get_filtered_file_paths(
                req.text_filter, req.star_states
            )
            return protocol.GetFilteredFilePathsResponse(paths=visible_paths)

        elif command == "get_directory_files":
            req = protocol.GetDirectoryFilesRequest.model_validate(request_data)
            if not req.session_id:
                return protocol.ErrorResponse(message="get_directory_files requires a non-empty session_id.")
            # A new directory load from the GUI defines the active session.
            with self.session_lock:
                self.active_gui_session_id = req.session_id
            logging.info(f"Set active GUI session to {req.session_id[:8]} for path '{req.path}'")

            # A new GUI load cancels any active watchdog initial scan.
            render_manager = self.thumbnail_manager.render_manager
            for job_id in render_manager.get_all_job_ids():
                if job_id.startswith("watchdog::initial_scan"):
                    render_manager.cancel_job(job_id)

            # --- Discovery + Task Creation ---
            # Job 1 (fast scan): dedicated OS thread — iterates the directory and streams
            # scan_progress notifications without touching the worker pool.
            if self._fast_scan_cancel:
                self._fast_scan_cancel.set()
            cancel_event = threading.Event()
            self._fast_scan_cancel = cancel_event
            threading.Thread(
                target=self._run_fast_scan_thread,
                args=(req.session_id, req.path, req.recursive, cancel_event),
                daemon=True,
                name=f"FastScan-{req.session_id[:8]}",
            ).start()

            # Job 2: A lower-priority scan to create thumbnailing tasks for all images in the background.
            slow_scan_generator = self.directory_scanner.scan_incremental(
                req.path, req.recursive
            )
            slow_scan_job = SourceJob(
                job_id=f"gui_scan_tasks::{req.session_id}::{req.path}",
                priority=Priority.GUI_REQUEST_LOW,  # Runs after UI is populated & visible items are processed
                generator=slow_scan_generator,
                task_factory=self.thumbnail_manager.create_tasks_for_file,
                create_tasks=True  # Creates all backend tasks
            )
            self.thumbnail_manager.render_manager.submit_source_job(slow_scan_job)

            # Job 3 (Stage C): Background view image generation.
            # Runs at BACKGROUND_SCAN (10), well below thumbnail tasks (40/90), so it
            # only consumes workers after the queue has no pending thumbnail work.
            view_image_generator = self.directory_scanner.scan_incremental(
                req.path, req.recursive
            )
            view_image_job = SourceJob(
                job_id=f"gui_view_images::{req.session_id}::{req.path}",
                priority=Priority.BACKGROUND_SCAN,
                generator=view_image_generator,
                task_factory=self.thumbnail_manager.create_view_image_task_for_file,
                create_tasks=True
            )
            self.thumbnail_manager.render_manager.submit_source_job(view_image_job)

            # Return cached files immediately while the scan runs in the background
            db_files = self.thumbnail_manager.metadata_db.get_directory_files(req.path)
            if db_files:
                logging.info(f"Found {len(db_files)} files in DB for '{req.path}'. Returning cached list while scan runs.")
                if not req.recursive:
                    normalized_path = os.path.normpath(req.path)
                    db_files = [f for f in db_files if os.path.dirname(f) == normalized_path]
                return protocol.GetDirectoryFilesResponse(files=sorted(db_files))

            return protocol.GetDirectoryFilesResponse(files=[])

        elif command == "move_records":
            req = protocol.MoveRecordsRequest.model_validate(request_data)
            count = self.thumbnail_manager.metadata_db.move_records(req.moves)
            return protocol.MoveRecordsResponse(moved_count=count)

        return protocol.ErrorResponse(message=f"Unknown command: {command}")

    def send_notification(self, notification: protocol.Notification):
        """Sends a JSON notification to all registered listener clients."""
        with self.notification_lock:
            if not self.notification_clients:
                logging.debug(f"[notify] no clients registered, dropping {notification.type}")
                return

            logging.info(f"Sending notification to {len(self.notification_clients)} client(s): {notification.type}")
            data = notification.model_dump_json().encode()
            length_prefix = len(data).to_bytes(4, byteorder='big')
            message = length_prefix + data

            dead_clients = []
            for client in self.notification_clients:
                try:
                    client.sendall(message)
                except (OSError, BrokenPipeError) as e:
                    logging.warning(f"Failed to send notification to client, marking for removal: {e}")
                    dead_clients.append(client)

            for client in dead_clients:
                self.notification_clients.remove(client)

    def shutdown(self) -> None:
        """Stop the server and clean up resources."""
        if not self.running:
            return
        
        logging.info("ThumbnailSocketServer shutting down.")
        self.running = False
        if self.watchdog_handler:
            self.watchdog_handler.stop()
        try:
            # Close server socket
            self.server_socket.close()

            # Clean up socket file
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

        except Exception as e:  # why: OS errors closing socket or removing socket file
            logging.error(f"Error during shutdown: {e}")
        # Forcibly exit the process in daemon mode to ensure it terminates
        if self.is_daemon_mode:
            logging.info("Forcing daemon process exit.")
            sys.exit(0)
