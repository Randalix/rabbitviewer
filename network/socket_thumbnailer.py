import os
import socket
import json
import logging
from typing import Optional
import threading
import time
from filewatcher.watcher import WatchdogHandler
import sys
from core.directory_scanner import DirectoryScanner, ReconcileContext
from core.rendermanager import Priority, TaskType, SourceJob
from . import protocol
from ._framing import MAX_MESSAGE_SIZE
_ValidationErrors = (ValueError, TypeError, KeyError)
import queue

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
        self._compound_task_counter = 0
        self._command_handlers = {
            "request_previews":      self._handle_request_previews,
            "get_previews_status":   self._handle_get_previews_status,
            "get_metadata_batch":    self._handle_get_metadata_batch,
            "set_rating":            self._handle_set_rating,
            "shutdown":              self._handle_shutdown,
            "update_viewport":       self._handle_update_viewport,
            "request_view_image":    self._handle_request_view_image,
            "get_filtered_file_paths": self._handle_get_filtered_file_paths,
            "get_directory_files":   self._handle_get_directory_files,
            "move_records":          self._handle_move_records,
            "run_tasks":             self._handle_run_tasks,
            "set_tags":              self._handle_set_tags,
            "remove_tags":           self._handle_remove_tags,
            "get_tags":              self._handle_get_tags,
            "get_image_tags":        self._handle_get_image_tags,
        }

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

                    # If a GUI client disconnects, demote session-scoped jobs
                    # to ORPHAN_SCAN so they finish in the background and
                    # populate the DB cache for the next connect.
                    with self.session_lock:
                        active_session = self.active_gui_session_id
                    if active_session:
                        render_manager = self.thumbnail_manager.render_manager
                        _GUI_JOB_PREFIXES = ("gui_scan", "post_scan")
                        jobs_to_demote = [
                            job_id for job_id in render_manager.get_all_job_ids()
                            if job_id.startswith(_GUI_JOB_PREFIXES) and active_session in job_id
                        ]
                        for job_id in jobs_to_demote:
                            logging.info(f"Client for session {active_session[:8]} disconnected. Demoting job: {job_id}")
                            render_manager.demote_job(job_id, Priority.ORPHAN_SCAN)

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
        try:
            command = request_data.get("command")
            if not command:
                return protocol.ErrorResponse(message="Request missing 'command' field.").model_dump_json()

            # --- Command Dispatcher ---
            response_model = self._dispatch_command(command, request_data)
            return response_model.model_dump_json()

        except _ValidationErrors as e:
            return protocol.ErrorResponse(message=f"Validation Error: {e}").model_dump_json()
        except Exception as e:  # why: any unhandled error from handler dispatch must not crash the server
            logging.error(f"Error processing request: {e}", exc_info=True)
            return protocol.ErrorResponse(message=f"Internal Server Error: {str(e)}").model_dump_json()
    
    def _dispatch_command(self, command: str, request_data: dict) -> protocol.Response:
        """Dispatches commands to the appropriate handler."""
        handler = self._command_handlers.get(command)
        if handler is None:
            return protocol.ErrorResponse(message=f"Unknown command: {command}")
        return handler(request_data)

    def _get_session_id(self) -> Optional[str]:
        with self.session_lock:
            return self.active_gui_session_id

    # ──────────────────────────────────────────────────────────────────────
    #  Command handlers
    # ──────────────────────────────────────────────────────────────────────

    def _handle_request_previews(self, request_data: dict) -> protocol.Response:
        req = protocol.RequestPreviewsRequest.model_validate(request_data)
        logging.info(f"SocketServer: Received request_previews for {len(req.image_paths)} paths with priority {req.priority}.")
        session_id = self._get_session_id()
        priority_level = Priority(req.priority)
        success_count = self.thumbnail_manager.batch_request_thumbnails(
            req.image_paths, priority_level, session_id
        )
        return protocol.RequestPreviewsResponse(count=success_count)

    def _handle_get_previews_status(self, request_data: dict) -> protocol.Response:
        req = protocol.GetPreviewsStatusRequest.model_validate(request_data)
        statuses = {}
        for path in req.image_paths:
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

    def _handle_get_metadata_batch(self, request_data: dict) -> protocol.Response:
        req = protocol.GetMetadataBatchRequest.model_validate(request_data)

        if req.priority:
            self.thumbnail_manager.render_manager.submit_task(
                f"metadata_batch::{hash(tuple(req.image_paths))}",
                Priority.GUI_REQUEST,
                self.thumbnail_manager.request_metadata_extraction,
                req.image_paths, Priority.GUI_REQUEST,
                task_type=TaskType.SIMPLE
            )

        metadata_results = {}
        for path in req.image_paths:
            metadata = self.thumbnail_manager.metadata_db.get_metadata(path)
            metadata_results[path] = metadata if metadata else {}

        return protocol.GetMetadataBatchResponse(metadata=metadata_results)

    def _handle_set_rating(self, request_data: dict) -> protocol.Response:
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
        return protocol.ErrorResponse(message="Failed to update rating in database.")

    def _handle_shutdown(self, request_data: dict) -> protocol.Response:
        self.shutdown()
        return protocol.Response(message="Server shutting down")

    def _handle_update_viewport(self, request_data: dict) -> protocol.Response:
        req = protocol.UpdateViewportRequest.model_validate(request_data)
        session_id = self._get_session_id()

        success_count = 0
        for pp in req.paths_to_upgrade:
            if self.thumbnail_manager.request_thumbnail(
                    pp.path, Priority(pp.priority), session_id):
                success_count += 1

        if req.paths_to_downgrade:
            self.thumbnail_manager.downgrade_thumbnail_tasks(
                req.paths_to_downgrade, Priority.GUI_REQUEST_LOW)

        if req.fullres_to_cancel:
            self.thumbnail_manager.cancel_speculative_fullres_batch(
                req.fullres_to_cancel)

        for pp in req.fullres_to_request:
            self.thumbnail_manager.request_speculative_fullres(
                pp.path, Priority(pp.priority), session_id)

        return protocol.Response(message=f"{success_count} upgraded")

    def _handle_request_view_image(self, request_data: dict) -> protocol.Response:
        req = protocol.RequestViewImageRequest.model_validate(request_data)
        logging.info(f"SocketServer: Received request_view_image for {req.image_path}")
        view_image_path = self.thumbnail_manager.request_view_image(
            req.image_path, self._get_session_id()
        )
        return protocol.RequestViewImageResponse(view_image_path=view_image_path)

    def _handle_get_filtered_file_paths(self, request_data: dict) -> protocol.Response:
        req = protocol.GetFilteredFilePathsRequest.model_validate(request_data)
        visible_paths = self.thumbnail_manager.metadata_db.get_filtered_file_paths(
            req.text_filter, req.star_states,
            tag_names=req.tag_names if req.tag_names else None,
        )
        return protocol.GetFilteredFilePathsResponse(paths=visible_paths)

    def _handle_get_directory_files(self, request_data: dict) -> protocol.Response:
        req = protocol.GetDirectoryFilesRequest.model_validate(request_data)
        if not req.session_id:
            return protocol.ErrorResponse(message="get_directory_files requires a non-empty session_id.")
        # A new directory load from the GUI defines the active session.
        with self.session_lock:
            self.active_gui_session_id = req.session_id
        logging.info(f"Set active GUI session to {req.session_id[:8]} for path '{req.path}'")

        # Phase 1: Return DB-cached files immediately (now recursive-aware).
        db_files = self.thumbnail_manager.metadata_db.get_directory_files(
            req.path, recursive=req.recursive
        )
        logging.info(
            f"DB returned {len(db_files)} cached files for '{req.path}' "
            f"(recursive={req.recursive}). Starting reconciliation walk."
        )

        # Batch-fetch cached thumbnail paths so the GUI can load them
        # directly from local cache without a daemon round-trip.
        thumb_map: dict[str, str] = {}
        if db_files:
            t0 = time.perf_counter()
            validity = self.thumbnail_manager.metadata_db.batch_get_cached_thumbnail_validity(db_files)
            thumb_map = {
                fp: info['thumbnail_path']
                for fp, info in validity.items()
                if info.get('valid') and info.get('thumbnail_path')
            }
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logging.info(
                f"[startup] batch thumbnail lookup: {len(thumb_map)}/{len(db_files)} "
                f"cached in {elapsed_ms:.0f} ms"
            )

        # Phase 2: Single reconciliation walk — discovers new files,
        # creates thumbnail + metadata + view-image tasks, and detects
        # ghost files (in DB but deleted on disk).
        reconcile_ctx = ReconcileContext(db_file_set=set(db_files))
        rm = self.thumbnail_manager.render_manager
        session_id = req.session_id

        def _on_reconcile_complete():
            if reconcile_ctx.ghost_files:
                logging.info(
                    f"Reconciliation found {len(reconcile_ctx.ghost_files)} "
                    f"ghost files for '{req.path}'."
                )
                notification = protocol.Notification(
                    type="files_removed",
                    data=protocol.FilesRemovedData(
                        files=reconcile_ctx.ghost_files
                    ).model_dump(),
                    session_id=session_id,
                )
                try:
                    rm.notification_queue.put_nowait(notification)
                except queue.Full:
                    logging.warning("Notification queue full; dropping files_removed.")
                self.thumbnail_manager.metadata_db.remove_records(
                    reconcile_ctx.ghost_files
                )

            # Phase 3: Now that the scan is complete, create thumbnail + metadata +
            # view-image tasks for all discovered files at LOW priority.  The heatmap
            # will upgrade visible ones; everything else processes in the background.
            # If the GUI disconnected during the scan, demote to ORPHAN_SCAN so
            # the tasks still finish but don't compete with future GUI work.
            discovered = reconcile_ctx.discovered_files
            if discovered:
                with self.session_lock:
                    is_orphaned = self.active_gui_session_id != session_id
                scan_task_priority = Priority.ORPHAN_SCAN if is_orphaned else Priority.LOW

                logging.info(
                    f"Post-scan: creating tasks for {len(discovered)} "
                    f"discovered files in '{req.path}'"
                    f"{' (orphaned)' if is_orphaned else ''}."
                )
                def _discovered_batch_generator():
                    batch = []
                    for f in discovered:
                        batch.append(f)
                        if len(batch) >= 10:
                            yield batch
                            batch = []
                    if batch:
                        yield batch

                task_job = SourceJob(
                    job_id=f"post_scan::{session_id}::{req.path}",
                    priority=scan_task_priority,
                    task_priority=scan_task_priority,
                    generator=_discovered_batch_generator(),
                    task_factory=self.thumbnail_manager.create_gui_tasks_for_file,
                    create_tasks=True,
                )
                rm.submit_source_job(task_job)

        reconcile_job = SourceJob(
            job_id=f"gui_scan::{req.session_id}::{req.path}",
            priority=Priority(80),
            generator=self.directory_scanner.scan_incremental_reconcile(
                req.path, req.recursive, reconcile_ctx
            ),
            task_factory=self.thumbnail_manager.create_gui_tasks_for_file,
            create_tasks=False,
            on_complete=_on_reconcile_complete,
        )
        rm.submit_source_job(reconcile_job)

        return protocol.GetDirectoryFilesResponse(
            files=sorted(db_files),
            thumbnail_paths=thumb_map,
        )

    def _handle_move_records(self, request_data: dict) -> protocol.Response:
        req = protocol.MoveRecordsRequest.model_validate(request_data)
        count = self.thumbnail_manager.metadata_db.move_records(req.moves)
        return protocol.MoveRecordsResponse(moved_count=count)

    def _handle_run_tasks(self, request_data: dict) -> protocol.Response:
        req = protocol.RunTasksRequest.model_validate(request_data)
        if not req.operations:
            return protocol.ErrorResponse(message="run_tasks requires at least one operation")
        for op in req.operations:
            if self.thumbnail_manager.get_task_operation(op.name) is None:
                return protocol.ErrorResponse(message=f"Unknown task operation: {op.name}")
        self._compound_task_counter += 1
        task_id = f"script_task::{self._compound_task_counter}"
        operations = [(op.name, op.file_paths) for op in req.operations]
        queued = self.thumbnail_manager.render_manager.submit_task(
            task_id,
            Priority.NORMAL,
            self.thumbnail_manager.execute_compound_task,
            operations,
        )
        if not queued:
            return protocol.ErrorResponse(message=f"Failed to queue compound task: {task_id}")
        return protocol.RunTasksResponse(task_id=task_id, queued_count=len(req.operations))

    def _handle_set_tags(self, request_data: dict) -> protocol.Response:
        req = protocol.SetTagsRequest.model_validate(request_data)
        db = self.thumbnail_manager.metadata_db
        success = db.batch_set_tags(req.image_paths, req.tags)
        if success:
            for path in req.image_paths:
                all_tags = db.get_image_tags(path)
                self.thumbnail_manager.render_manager.submit_task(
                    f"write_tags::{path}",
                    Priority.NORMAL,
                    self.thumbnail_manager._write_tags_to_file,
                    path, all_tags,
                    task_type=TaskType.SIMPLE,
                )
            return protocol.Response(message="Tags updated and queued for file write.")
        return protocol.ErrorResponse(message="Failed to update tags in database.")

    def _handle_remove_tags(self, request_data: dict) -> protocol.Response:
        req = protocol.RemoveTagsRequest.model_validate(request_data)
        db = self.thumbnail_manager.metadata_db
        success = db.batch_remove_tags(req.image_paths, req.tags)
        if success:
            for path in req.image_paths:
                all_tags = db.get_image_tags(path)
                self.thumbnail_manager.render_manager.submit_task(
                    f"write_tags::{path}",
                    Priority.NORMAL,
                    self.thumbnail_manager._write_tags_to_file,
                    path, all_tags,
                    task_type=TaskType.SIMPLE,
                )
            return protocol.Response(message="Tags removed and queued for file write.")
        return protocol.ErrorResponse(message="Failed to remove tags from database.")

    def _handle_get_tags(self, request_data: dict) -> protocol.Response:
        req = protocol.GetTagsRequest.model_validate(request_data)
        db = self.thumbnail_manager.metadata_db
        all_tags = db.get_all_tags()
        dir_tags = db.get_directory_tags(req.directory_path) if req.directory_path else []
        return protocol.GetTagsResponse(
            directory_tags=[protocol.TagInfo(name=t['name'], kind=t['kind']) for t in dir_tags],
            global_tags=[protocol.TagInfo(name=t['name'], kind=t['kind']) for t in all_tags],
        )

    def _handle_get_image_tags(self, request_data: dict) -> protocol.Response:
        req = protocol.GetImageTagsRequest.model_validate(request_data)
        db = self.thumbnail_manager.metadata_db
        result = {path: db.get_image_tags(path) for path in req.image_paths}
        return protocol.GetImageTagsResponse(tags=result)

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
