"""Central typed notification hub for daemon→GUI messages.

DaemonSignals(QObject) exposes one Qt Signal per notification type.
Thread bridging is handled automatically by Qt's QueuedConnection: dispatch()
is called from a background thread, and connected slots run on the receiver's
thread (the main thread for all GUI subscribers).
"""
import logging
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

from core.notifications import (
    Notification,
    PreviewsReadyData,
    ScanProgressData,
    ScanCompleteData,
    FilesRemovedData,
    ClipIndexProgressData,
    ComfyUICompleteData,
)

_ValidationErrors = (ValueError, TypeError, KeyError)


class DaemonSignals(QObject):
    """One typed Signal per daemon notification type.

    Create one instance after QApplication exists and pass it to both
    the RenderManager notification callback and each GUI subscriber.
    """

    previews_ready      = Signal(object)  # PreviewsReadyData
    scan_progress       = Signal(object)  # ScanProgressData
    scan_complete       = Signal(object)  # ScanCompleteData
    files_removed       = Signal(object)  # FilesRemovedData
    clip_index_progress = Signal(object)  # ClipIndexProgressData
    comfyui_complete    = Signal(object)  # ComfyUICompleteData

    def dispatch_notification(self, notification: Notification) -> None:
        """Accept a Notification object and emit the matching signal.

        Called from RenderManager worker threads via the notification callback.
        The notification.data dict is validated into the appropriate typed
        dataclass before emission.
        """
        self.dispatch(notification.type, notification.data)

    def dispatch(self, notification_type: str, data: dict) -> None:
        """Validate *data* and emit the matching signal.

        Called from a background thread. Qt delivers connected slots on their
        owner thread via AutoConnection.
        """
        try:
            match notification_type:
                case "previews_ready":
                    self.previews_ready.emit(PreviewsReadyData.model_validate(data))
                case "scan_progress":
                    self.scan_progress.emit(ScanProgressData.model_validate(data))
                case "scan_complete":
                    self.scan_complete.emit(ScanCompleteData.model_validate(data))
                case "files_removed":
                    self.files_removed.emit(FilesRemovedData.model_validate(data))
                case "clip_index_progress":
                    self.clip_index_progress.emit(ClipIndexProgressData.model_validate(data))
                case "comfyui_complete":
                    self.comfyui_complete.emit(ComfyUICompleteData.model_validate(data))
                case _:
                    logger.debug("DaemonSignals: unknown notification type %r", notification_type)
        except _ValidationErrors as e:
            logger.error(
                "DaemonSignals: failed to validate %r notification: %s",
                notification_type, e,
            )
