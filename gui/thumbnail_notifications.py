from __future__ import annotations
import os
import time
import logging
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Slot

from core.notifications import PreviewsReadyData, ScanProgressData, ScanCompleteData, FilesRemovedData

if TYPE_CHECKING:
    from gui.filter_controller import FilterController
    from gui.thumbnail_model import ThumbnailModel
    from gui.viewport_prioritizer import ViewportPrioritizer
    from network.daemon_signals import DaemonSignals

logger = logging.getLogger(__name__)


class NotificationHandler(QObject):

    def __init__(self, widget, model: ThumbnailModel,
                 prioritizer: ViewportPrioritizer,
                 filter_controller: FilterController):
        super().__init__(parent=widget)
        self._widget = widget
        self.model = model
        self.prioritizer = prioritizer
        self._filter_controller = filter_controller

        self._daemon_signals: Optional[DaemonSignals] = None

        # Scan-batch coalescing state
        self.scan_batch_pending = False
        self.scan_first_batch_flushed = False

        # Startup timing
        self.startup_t0: Optional[float] = None
        self._startup_first_scan_progress = False
        self._startup_first_previews_ready = False

        # Timers
        self.preview_tick_timer = QTimer(self)
        self.preview_tick_timer.setInterval(16)  # ~60 fps drain rate

        # singleShot: fires 250ms after first batch in a burst, NOT restarted
        # per batch, so flushes happen every ~250ms during continuous scanning.
        self._scan_coalesce_timer = QTimer(self)
        self._scan_coalesce_timer.setSingleShot(True)
        self._scan_coalesce_timer.setInterval(250)
        self._scan_coalesce_timer.timeout.connect(self._flush_scan_layout)

    # -- Daemon signal wiring ------------------------------------------------

    def set_daemon_signals(self, daemon_signals: DaemonSignals) -> None:
        self._daemon_signals = daemon_signals
        daemon_signals.previews_ready.connect(self._on_previews_ready)
        daemon_signals.scan_progress.connect(self._on_scan_progress)
        daemon_signals.files_removed.connect(self._on_files_removed)
        daemon_signals.scan_complete.connect(self._on_scan_complete)

    # -- Notification handlers -----------------------------------------------

    @Slot(object)
    def _on_previews_ready(self, data: PreviewsReadyData) -> None:
        logger.debug("[trace] previews_ready: all_files=%d, is_loading=%s", len(self.model.all_files), self._widget._is_loading)
        if not self._startup_first_previews_ready and self.startup_t0 is not None:
            self._startup_first_previews_ready = True
            elapsed_ms = (time.perf_counter() - self.startup_t0) * 1000
            logger.info("[startup] first previews_ready: %.0f ms after load_directory", elapsed_ms)
        image_path = data.image_entry.path
        logger.info("ThumbnailViewWidget received notification: Previews ready for %s", image_path)
        if data.thumbnail_path:
            # why: daemon watchdog/prior sessions emit previews_ready for
            # stale paths; skip to avoid wasting tick slots.
            if image_path not in self.model.path_to_idx:
                # Check if this image lives inside a folder card — if so, feed
                # the thumbnail into the card's preview mosaic.
                parent = os.path.dirname(image_path)
                node = self.model.folder_nodes.get(parent)
                if node and len(node.preview_paths) < 4 and data.thumbnail_path not in node.preview_paths:
                    node.preview_paths.append(data.thumbnail_path)
                    folder_idx = self.model.path_to_idx.get(parent, -1)
                    label = self._widget.labels.get(folder_idx) if folder_idx >= 0 else None
                    if label:
                        label._folder_preview_pixmaps = None  # invalidate cached mosaic
                        label._cache_folder_previews()
                        label.update()
                return
            self.prioritizer.queue_previews([(image_path, data.thumbnail_path)])
            if not self.preview_tick_timer.isActive():
                self.preview_tick_timer.start()
        else:
            logger.debug("[thumb] previews_ready has no thumbnail_path for %s", os.path.basename(image_path))

    @Slot(object)
    def _on_scan_progress(self, data: ScanProgressData) -> None:
        if not self._widget.path_belongs_to_current_directory(data.path):
            return
        logger.debug("[trace] scan_progress: all_files=%d, is_loading=%s", len(self.model.all_files), self._widget._is_loading)
        try:
            first_batch = not self._startup_first_scan_progress
            if first_batch and self.startup_t0 is not None:
                self._startup_first_scan_progress = True
                elapsed_ms = (time.perf_counter() - self.startup_t0) * 1000
                logger.info("[startup] first scan_progress: %.0f ms after load_directory (%d files in batch)", elapsed_ms, len(data.files))
            logger.info("Received scan_progress batch for '%s' with %d files.", data.path, len(data.files))
            self._widget._add_image_batch(sorted(f.path for f in data.files))
            # Mark that the first layout after this batch should seed the
            # heatmap immediately.  We cannot call _prioritize_visible_thumbnails
            # here because model.visible_to_original is not yet populated —
            # label creation and layout update happen asynchronously via timers.
            if first_batch:
                self._widget._needs_heatmap_seed = True
        except Exception as e:
            # why: protocol extensions in future daemon versions may produce
            # unexpected field types; isolate to prevent notification loop crash.
            logger.error("Unexpected exception in scan_progress handler: %s", e, exc_info=True)

    @Slot(object)
    def _on_files_removed(self, data: FilesRemovedData) -> None:
        logger.debug("[trace] files_removed: all_files=%d, is_loading=%s", len(self.model.all_files), self._widget._is_loading)
        if data.files:
            logger.info("Removing %d ghost files from view.", len(data.files))
            self._widget.remove_images([f.path for f in data.files])

    @Slot(object)
    def _on_scan_complete(self, data: ScanCompleteData) -> None:
        logger.debug("[trace] scan_complete: all_files=%d, is_loading=%s", len(self.model.all_files), self._widget._is_loading)
        if not self._widget.path_belongs_to_current_directory(data.path):
            logger.debug("Ignoring scan_complete for '%s' (current directory: '%s')", data.path, self.model.current_directory_path)
            return
        if self.startup_t0 is not None:
            elapsed_ms = (time.perf_counter() - self.startup_t0) * 1000
            logger.info("[startup] scan_complete: %.0f ms after load_directory", elapsed_ms)
        logger.info(
            "[virtual] scan_complete: all_files=%d, labels=%d, current_files(in layout)=%d",
            len(self.model.all_files), len(self._widget.labels), len(self.model.current_files),
        )
        self._filter_controller.reset()
        self._scan_coalesce_timer.stop()
        self.model.scan_active = False
        self._widget._is_loading = False
        self.scan_batch_pending = False

        if not self.model.hidden_indices and self._widget._virtual_grid:
            # No filter active: data structures are fully populated by the
            # append-only fast path.  Do one final sorted reorder (no-op if
            # all_files is already sorted) and snap the container height.
            # Skip if a script has applied a custom sort — resorting would undo it.
            top_file = self._widget._get_first_visible_file()
            logger.info(
                "[virtual] scan_complete: final sort, top_file=%s, all_files=%d, custom_sort=%s",
                os.path.basename(top_file) if top_file else None, len(self.model.all_files),
                self._widget._custom_sort_active,
            )
            self._widget._virtual_grid.snap_height_to_exact()
            if not self._widget._custom_sort_active:
                self._widget.reorder_files(sorted(self.model.all_files))
            if top_file:
                self._widget.scroll_to_top(top_file)
                self._widget._sync_virtual_viewport()
            QTimer.singleShot(100, self._widget._prioritize_visible_thumbnails)
        else:
            # Filter active: full rebuild unavoidable.
            self._filter_controller.reapply_filters()

    # -- Scan coalescing -----------------------------------------------------

    def _flush_scan_layout(self):
        if not self.scan_batch_pending or not self._widget._virtual_grid:
            return
        self.scan_batch_pending = False
        self.scan_first_batch_flushed = True
        logger.info(
            "[virtual] _flush_scan_layout: current_files=%d, all_files=%d",
            len(self.model.current_files), len(self.model.all_files),
        )
        self._widget._virtual_grid.set_total_items_chunked(len(self.model.current_files))
        # why: skip update_layout() here — column recalculation only matters
        # on resize, which is handled by the dedicated resize timer.  Calling
        # it every 250ms during a scan triggers O(n) label.move() no-ops.
        self._widget._sync_virtual_viewport()
        self.model.last_layout_file_count = len(self.model.all_files)

    # -- Lifecycle -----------------------------------------------------------

    def reset_startup(self, t0: float):
        self.startup_t0 = t0
        self._startup_first_scan_progress = False
        self._startup_first_previews_ready = False

    def reset(self):
        self.scan_batch_pending = False
        self.scan_first_batch_flushed = False
        self._scan_coalesce_timer.stop()

    def dispose(self):
        if self._daemon_signals:
            self._daemon_signals.previews_ready.disconnect(self._on_previews_ready)
            self._daemon_signals.scan_progress.disconnect(self._on_scan_progress)
            self._daemon_signals.files_removed.disconnect(self._on_files_removed)
            self._daemon_signals.scan_complete.disconnect(self._on_scan_complete)
        self._scan_coalesce_timer.stop()
        self.preview_tick_timer.stop()
