from __future__ import annotations
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Signal, Slot

import os
from core.event_system import (event_system, EventType, StatusMessageEventData)
from core.priority import Priority

if TYPE_CHECKING:
    from gui.thumbnail_model import ThumbnailModel

logger = logging.getLogger(__name__)


def _submit_face_detection_if_needed(service, directory: str, all_files: list) -> None:
    """Worker-thread: submit face detection for unindexed files (on-demand only).

    Called when the user activates a face-person filter. The daemon handles
    bulk indexing; this covers images that arrived while the GUI was open.
    """
    try:
        service.index_faces_on_demand(directory, all_files)
    except Exception:
        logger.debug("_submit_face_detection_if_needed failed", exc_info=True)


class FilterController(QObject):

    _filtered_paths_ready = Signal(object)
    filters_applied = Signal()

    def __init__(
        self,
        parent: QObject,
        model: ThumbnailModel,
        executor: ThreadPoolExecutor,
        *,
        is_loading: Callable[[], bool],
        label_count: Callable[[], int],
        on_layout_rebuilt: Callable[[], None],
    ):
        super().__init__(parent=parent)
        self.model = model
        self._executor = executor
        self.service = None
        self._is_loading = is_loading
        self._label_count = label_count
        self._on_layout_rebuilt = on_layout_rebuilt
        self._phash_progress_subscribed = False

        self._filter_in_flight = False
        self._filter_pending = False

        self._filter_update_timer = QTimer(self)
        self._filter_update_timer.setSingleShot(True)
        self._filter_update_timer.setInterval(80)
        self._filter_update_timer.timeout.connect(self.reapply_filters)

        self._filtered_paths_ready.connect(self._on_filtered_paths_ready)

        event_system.subscribe(EventType.FACE_PERSON_FILTER, self._on_face_person_filter)
        event_system.subscribe(EventType.TEXT_FILTER_CHANGED, self._on_text_filter_event)
        event_system.subscribe(EventType.STAR_FILTER_CHANGED, self._on_star_filter_event)
        event_system.subscribe(EventType.TAG_FILTER_CHANGED, self._on_tag_filter_event)
        event_system.subscribe(EventType.DUPLICATES_FILTER_CHANGED, self._on_duplicates_filter_event)
        event_system.subscribe(EventType.TOGGLE_DUPLICATES_FILTER, self._on_toggle_duplicates_filter)
        event_system.subscribe(EventType.DATE_FILTER_CHANGED, self._on_date_filter_event)
        event_system.subscribe(EventType.CLEAR_FILTERS, self._on_clear_filters_event)

    # -- Public filter API ---------------------------------------------------

    def apply_filter(self, filter_text: str):
        self.model.set_text_filter(filter_text)
        self._filter_update_timer.start()

    def apply_star_filter(self, star_states: list):
        self.model.set_star_filter(star_states)
        self._filter_update_timer.start()

    def apply_tag_filter(self, tag_names: list):
        self.model.set_tag_filter(tag_names)
        self._filter_update_timer.start()

    def clear_filter(self):
        self.model.clear_filters()
        if self._phash_progress_subscribed:
            event_system.unsubscribe(EventType.PHASH_PROGRESS, self._on_phash_progress)
            self._phash_progress_subscribed = False
        # Apply the now-empty hidden set so rebuild_visible_mappings() runs
        # immediately — without this, compute_hidden_indices sees no change
        # (set() == set()) and skips the rebuild, leaving stale mappings.
        self.model.apply_hidden_indices(set())
        self._on_layout_rebuilt()
        self._filter_update_timer.start()

    def apply_clip_search_results(self, result_paths: list):
        self.model.set_clip_search_paths(set(result_paths))
        self._filter_update_timer.start()

    def clear_clip_search(self):
        self.model.set_clip_search_paths(None)
        self.model.apply_hidden_indices(set())
        self.reapply_filters()

    def apply_person_filter(self, file_paths: list):
        self.model.set_person_filter_paths(set(file_paths))
        self._filter_update_timer.start()

    def clear_person_filter(self):
        self.model.set_person_filter_paths(None)
        self.reapply_filters()

    def apply_date_filter(self, date_range: Optional[Tuple[float, float]]) -> None:
        self.model.set_date_filter(date_range)
        self._filter_update_timer.start()

    def apply_selection_filter(self, paths: set):
        self.model.set_selection_filter_paths(paths)
        self._filter_update_timer.start()

    def clear_selection_filter(self):
        self.model.set_selection_filter_paths(None)
        self.reapply_filters()

    def apply_similarity_filter(self, paths: set):
        self.model.set_similarity_filter_paths(paths)
        self._filter_update_timer.start()

    def clear_similarity_filter(self):
        self.model.set_similarity_filter_paths(None)
        self.reapply_filters()

    # -- EventSystem handlers ------------------------------------------------

    def _on_face_person_filter(self, event_data):
        person_ids = event_data.person_ids
        if not person_ids:
            self.clear_person_filter()
            return
        if not self.service:
            return

        # On-demand: kick off face detection for any unindexed files in the
        # current directory. The daemon handles bulk indexing; this covers the
        # case where the GUI is open and the user explicitly requests face search.
        directory = self.model.current_directory_path
        if directory and self.model.all_files:
            self._executor.submit(_submit_face_detection_if_needed,
                                  self.service, directory, list(self.model.all_files))

        file_paths = self.service.get_face_paths_for_persons(person_ids)
        self.apply_person_filter(file_paths or [])

    def _on_text_filter_event(self, event_data):
        self.apply_filter(event_data.filter_text)

    def _on_star_filter_event(self, event_data):
        self.apply_star_filter(event_data.star_states)

    def _on_tag_filter_event(self, event_data):
        self.apply_tag_filter(event_data.tag_names)

    def _on_date_filter_event(self, event_data):
        self.apply_date_filter(event_data.date_range)

    def _on_duplicates_filter_event(self, event_data):
        self._apply_duplicates_filter(event_data.duplicates_only)

    def _on_toggle_duplicates_filter(self, event_data):
        self._apply_duplicates_filter(not self.model.duplicates_only)

    def _apply_duplicates_filter(self, enabled: bool):
        self.model.set_duplicates_only(enabled)
        if enabled:
            self._request_phash_urgent()
            if not self._phash_progress_subscribed:
                event_system.subscribe(EventType.PHASH_PROGRESS, self._on_phash_progress)
                self._phash_progress_subscribed = True
        else:
            if self._phash_progress_subscribed:
                event_system.unsubscribe(EventType.PHASH_PROGRESS, self._on_phash_progress)
                self._phash_progress_subscribed = False
        self._filter_update_timer.start()

    def _on_phash_progress(self, event_data):
        # Debounce: restart the timer so the filter re-runs ~200ms after the
        # last pHash task in a burst completes.
        self._filter_update_timer.start()

    def _request_phash_urgent(self):
        if not self.service or not self.model.all_files:
            return
        directory = self.model.current_directory_path
        if not directory:
            # Derive from first file path as fallback
            first = next(iter(self.model.all_files), None)
            directory = os.path.dirname(first) if first else None
        if not directory:
            return
        self.service.request_phash_batch(
            list(self.model.all_files), directory, Priority.GUI_REQUEST)

    def _on_clear_filters_event(self, event_data):
        self.clear_filter()

    # -- Filter orchestration ------------------------------------------------

    def reapply_filters(self):
        logger.info(
            "reapply_filters: is_loading=%s, all_files=%d, labels=%d",
            self._is_loading(), len(self.model.all_files), self._label_count(),
        )

        if not self.model.all_files or not self.service:
            logger.warning("Cannot apply filters: file list or service is not ready.")
            return

        if self._is_loading():
            # Fast path: show everything during the initial scan.
            visible = set(self.model.all_files)
            if self.model.clip_search_paths is not None:
                visible = visible & self.model.clip_search_paths
            if self.model.person_filter_paths is not None:
                visible = visible & self.model.person_filter_paths
            if self.model.selection_filter_paths is not None:
                visible = visible & self.model.selection_filter_paths
            if self.model.similarity_filter_paths is not None:
                visible = visible & self.model.similarity_filter_paths
            self._apply_filter_results(visible)
            return

        # Async path: submit the socket call to the executor so the GUI
        # stays responsive while the daemon processes the query.
        if self._filter_in_flight:
            self._filter_pending = True
            return

        self._filter_in_flight = True
        # Snapshot the current filter values so racing changes don't corrupt
        # the background call with half-old / half-new state.
        text_filter = self.model.current_filter
        star_filter = list(self.model.current_star_filter)
        tag_filter = list(self.model.current_tag_filter)
        duplicates_only = self.model.duplicates_only
        date_range = self.model.current_date_filter
        directory = self.model.current_directory_path
        is_recursive = self.model.is_recursive
        self._executor.submit(self._fetch_filtered_paths, text_filter, star_filter, tag_filter, duplicates_only, date_range, directory, is_recursive)

    def _fetch_filtered_paths(self, text_filter: str, star_filter: list, tag_filter: list, duplicates_only: bool = False, date_range: Optional[Tuple[float, float]] = None, directory: Optional[str] = None, is_recursive: bool = True):
        try:
            response = self.service.get_filtered_file_paths(
                text_filter, star_filter, tag_names=tag_filter or None,
                duplicates_only=duplicates_only, date_range=date_range,
                directory=directory, recursive=is_recursive,
            )
            if response is None:
                logger.error("Failed to get filtered paths.")
                self._filtered_paths_ready.emit(None)
                return

            result = set(response)

            if duplicates_only:
                # Union exact-hash duplicates with pHash near-duplicates
                phash_dupes = self.service.get_phash_duplicate_paths(list(self.model.all_files))
                result = result | phash_dupes

            if self._filtered_paths_ready is not None:
                if self._filtered_paths_ready is not None:
                    self._filtered_paths_ready.emit(result)
                else:
                    logger.error("Tried to emit _filtered_paths_ready but signal is None")
            else:
                logger.error("Tried to emit _filtered_paths_ready but signal is None")
        except Exception as e:
            # why: service calls can raise; broad guard ensures
            # _filtered_paths_ready always fires to unlock _filter_in_flight.
            logger.error("Error fetching filtered paths: %s", e, exc_info=True)
            self._filtered_paths_ready.emit(None)

    @Slot(object)
    def _on_filtered_paths_ready(self, visible_paths):
        self._filter_in_flight = False

        try:
            if visible_paths is None:
                visible_paths = set(self.model.all_files)

            if self.model.clip_search_paths is not None:
                visible_paths = visible_paths & self.model.clip_search_paths
            if self.model.person_filter_paths is not None:
                visible_paths = visible_paths & self.model.person_filter_paths
            if self.model.selection_filter_paths is not None:
                visible_paths = visible_paths & self.model.selection_filter_paths
            if self.model.similarity_filter_paths is not None:
                visible_paths = visible_paths & self.model.similarity_filter_paths

            self._apply_filter_results(visible_paths)
        except Exception as e:
            logger.error(
                "Error processing filtered paths: %s",
                str(e),
                exc_info=True
            )
            # Fallback to showing all files
            visible_paths = set()
            self._apply_filter_results(visible_paths)
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="thumbnail_view",
                timestamp=time.time(),
                message="Error applying filters - showing all files",
                timeout=4000
            ))

        # If another filter change arrived while this one was in flight,
        # re-submit with the latest filter values.
        if self._filter_pending:
            self._filter_pending = False
            self.reapply_filters()

    def _apply_filter_results(self, visible_paths: set):
        new_hidden, will_update = self.model.compute_hidden_indices(visible_paths)

        logger.info(
            "_apply_filter_results: all_files=%d, visible_paths=%d, "
            "hidden=%d, will_update=%s",
            len(self.model.all_files), len(visible_paths), len(new_hidden),
            will_update,
        )

        if will_update:
            self.model.apply_hidden_indices(new_hidden)
            self._on_layout_rebuilt()
            logger.info(
                "filter layout done: current_files=%d, materialized_labels=%d",
                len(self.model.current_files), self._label_count(),
            )

        total_count = len(self.model.all_files)
        visible_count = len(self.model.current_files)
        hidden_count = total_count - visible_count
        active = []
        if self.model.current_filter:
            active.append(f"text='{self.model.current_filter}'")
        if not all(self.model.current_star_filter):
            active.append("stars")
        if self.model.current_tag_filter:
            active.append("tags")
        if self.model.duplicates_only:
            active.append("duplicates")
        if self.model.current_date_filter is not None:
            active.append("date")
        if self.model.clip_search_paths is not None:
            active.append("clip")
        if self.model.person_filter_paths is not None:
            active.append("faces")
        if self.model.selection_filter_paths is not None:
            active.append("selection")
        if self.model.similarity_filter_paths is not None:
            active.append("similarity")
        filter_desc = ", ".join(active) if active else "none"
        status_msg = f"Filter ({filter_desc}): {visible_count}/{total_count} shown"
        if hidden_count > 0:
            status_msg += f" ({hidden_count} hidden)"
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="thumbnail_view",
            timestamp=time.time(),
            message=status_msg,
            timeout=4000
        ))
        self.filters_applied.emit()

    def has_active_filter(self) -> bool:
        """True if any filter is set (service-based or in-memory)."""
        if self.model.current_filter:
            return True
        if not all(self.model.current_star_filter):
            return True
        if self.model.current_tag_filter:
            return True
        if self.model.duplicates_only:
            return True
        if self.model.current_date_filter is not None:
            return True
        if self.model.clip_search_paths is not None:
            return True
        if self.model.person_filter_paths is not None:
            return True
        if self.model.selection_filter_paths is not None:
            return True
        if self.model.similarity_filter_paths is not None:
            return True
        return False

    # -- Lifecycle -----------------------------------------------------------

    def start_filter_timer(self):
        self._filter_update_timer.start()

    def stop_filter_timer(self):
        self._filter_update_timer.stop()

    def reset(self):
        self._filter_update_timer.stop()
        self._filter_in_flight = False
        self._filter_pending = False

    def dispose(self):
        event_system.unsubscribe(EventType.FACE_PERSON_FILTER, self._on_face_person_filter)
        event_system.unsubscribe(EventType.TEXT_FILTER_CHANGED, self._on_text_filter_event)
        event_system.unsubscribe(EventType.STAR_FILTER_CHANGED, self._on_star_filter_event)
        event_system.unsubscribe(EventType.TAG_FILTER_CHANGED, self._on_tag_filter_event)
        event_system.unsubscribe(EventType.DUPLICATES_FILTER_CHANGED, self._on_duplicates_filter_event)
        event_system.unsubscribe(EventType.TOGGLE_DUPLICATES_FILTER, self._on_toggle_duplicates_filter)
        event_system.unsubscribe(EventType.DATE_FILTER_CHANGED, self._on_date_filter_event)
        event_system.unsubscribe(EventType.CLEAR_FILTERS, self._on_clear_filters_event)
        if self._phash_progress_subscribed:
            event_system.unsubscribe(EventType.PHASH_PROGRESS, self._on_phash_progress)
            self._phash_progress_subscribed = False
        self._filter_update_timer.stop()
