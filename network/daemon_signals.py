"""Central typed notification hub for daemon→GUI messages.

DaemonSignals(QObject) exposes one Qt Signal per notification type.
Thread bridging is handled automatically by Qt's QueuedConnection: dispatch()
is called from the NotificationListener background thread, and connected slots
run on the receiver's thread (the main thread for all GUI subscribers).
"""
import logging
from PySide6.QtCore import QObject, Signal

from .protocol import (
    PreviewsReadyData,
    ScanProgressData,
    ScanCompleteData,
    FilesRemovedData,
    ComfyUICompleteData,
)

_ValidationErrors = (ValueError, TypeError, KeyError)


class DaemonSignals(QObject):
    """One typed Signal per daemon notification type.

    Create one instance after QApplication exists and pass it to both
    NotificationListener (producer) and each GUI subscriber (consumer).
    """

    previews_ready   = Signal(object)  # PreviewsReadyData
    scan_progress    = Signal(object)  # ScanProgressData
    scan_complete    = Signal(object)  # ScanCompleteData
    files_removed    = Signal(object)  # FilesRemovedData
    comfyui_complete = Signal(object)  # ComfyUICompleteData

    def dispatch(self, notification_type: str, data: dict) -> None:
        """Validate *data* and emit the matching signal.

        Called from the NotificationListener thread. Qt delivers connected
        slots on their owner thread via AutoConnection.
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
                case "comfyui_complete":
                    self.comfyui_complete.emit(ComfyUICompleteData.model_validate(data))
                case _:
                    logging.debug("DaemonSignals: unknown notification type %r", notification_type)
        except _ValidationErrors as e:
            logging.error(
                "DaemonSignals: failed to validate %r notification: %s",
                notification_type, e,
            )
