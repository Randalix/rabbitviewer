from __future__ import annotations

from typing import Optional, Set, List, TYPE_CHECKING
import threading
from PySide6.QtWidgets import QMainWindow, QStackedWidget, QApplication
from PySide6.QtCore import Slot, QPointF, QTimer, Signal, QSettings
import logging
logger = logging.getLogger(__name__)
import os
import time

from .thumbnail_view import ThumbnailViewWidget
from .hotkey_manager import HotkeyManager
from .metadata_cache import MetadataCache
from .info_panel import InfoPanelShell, MetadataProvider
from .filter_dialog import FilterDialog
from .tag_editor_dialog import TagEditorDialog
from .tag_filter_dialog import TagFilterDialog
from .modal_menu import ModalMenu
from .hotkey_help_overlay import HotkeyHelpOverlay, show_at_startup
from .menu_registry import build_menus
from scripts.script_manager import ScriptManager, ScriptAPI
from core.event_system import (event_system, EventType, EventData, InspectorEventData,
    StatusMessageEventData, StatusSection)
from core.selection import SelectionState, SelectionProcessor, SelectionHistory
from network.gui_server import GuiServer
from network.daemon_signals import DaemonSignals

if TYPE_CHECKING:
    from .inspector_view import InspectorView
    from .picture_view import PictureView
    from .compare_view import CompareView

_VIDEO_EXTENSIONS = frozenset([
    '.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v',
    '.wmv', '.flv', '.mpg', '.mpeg', '.3gp', '.ts',
])


def _is_video(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext.lower() in _VIDEO_EXTENSIONS

class MainWindow(QMainWindow):
    _hover_rating_ready = Signal(str, int)  # (path, rating)
    _hover_metadata_ready = Signal(str)  # path — emitted after cache populated
    _tag_filter_ready = Signal(list, list)  # (dir_tags, global_tags)
    _tag_editor_ready = Signal(int, list, list, list)  # (count, common_tags, dir_tags, global_tags)
    _clip_results_ready = Signal(object)  # list of (path, score) or None
    def __init__(self, config_manager, service,
                 daemon_signals: DaemonSignals, *, debug_ui: bool = False):
        super().__init__()
        self.config_manager = config_manager
        self.service = service
        self.daemon_signals = daemon_signals

        from .tiling_manager import TilingManager

        self._tiling = TilingManager()
        self.setCentralWidget(self._tiling)

        self.stacked_widget = QStackedWidget()
        self._tiling.set_main_content(self.stacked_widget)

        self.status_bar = None

        self.thumbnail_view = None
        self.picture_view = None
        self.video_view = None
        self.compare_view: Optional[CompareView] = None
        self.current_hovered_image = None
        self.inspector_views: List[InspectorView] = []
        self._inspector_slot = 0
        self.face_palettes: List = []
        self._face_selection: List[str] = []  # persists across palette open/close
        self.metadata_cache = MetadataCache(self.service)
        self.info_panels: List[InfoPanelShell] = []
        self._info_panel_slot = 0

        self._setup_thumbnail_view()

        self.last_known_directory = None

        self.setAcceptDrops(True)
        self.setWindowTitle("Hey, RabbitViewer!")
        settings = QSettings("RabbitViewer", "MainWindow")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(800, 600)

        if debug_ui:
            self.setStyleSheet("QWidget { border: 1px solid red; }")

        self.filter_dialog = None
        self.breadcrumb_bar = None
        self.tag_editor_dialog = None
        self.tag_filter_dialog = None
        self.comfyui_dialog = None
        self.clip_search_dialog = None
        self._pre_clip_search_order = None
        self._removed_images = []

        QTimer.singleShot(0, self._deferred_init)

    def _deferred_init(self):
        from .status_bar import CustomStatusBar
        self.status_bar = CustomStatusBar(self.config_manager, self)
        self.setStatusBar(self.status_bar)

        self.selection_state = SelectionState()
        self.selection_processor = SelectionProcessor(self.selection_state)
        self.selection_history = SelectionHistory(self.selection_processor)

        self._gui_server = GuiServer(self)
        self._gui_server.start()

        self.script_manager = ScriptManager(self)
        self.script_api = ScriptAPI(self)
        scripts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
        self.script_manager.load_scripts(scripts_dir)

        self._setup_hotkeys()
        self.modal_menu = ModalMenu(self, build_menus(), self.script_manager)
        self._setup_event_subscriptions()

        if show_at_startup():
            QTimer.singleShot(0, self._toggle_hotkey_help)

    def _setup_thumbnail_view(self):
        self.thumbnail_view = ThumbnailViewWidget(self.config_manager)
        self.thumbnail_view.set_service(self.service)
        self.thumbnail_view.set_daemon_signals(self.daemon_signals)
        self.thumbnail_view.doubleClicked.connect(self._handle_thumbnail_double_click)
        self.thumbnail_view.folderNavigated.connect(self._handle_folder_navigation)
        self.thumbnail_view.benchmarkComplete.connect(self._handle_benchmark_result)
        self.stacked_widget.addWidget(self.thumbnail_view)

        self._hover_prefetch_path: Optional[str] = None
        self._last_rating_set_time: float = 0.0
        self._hover_prefetch_timer = QTimer(self)
        self._hover_prefetch_timer.setSingleShot(True)
        self._hover_prefetch_timer.setInterval(150)
        self._hover_prefetch_timer.timeout.connect(self._do_hover_prefetch)
        self._hover_clear_timer = QTimer(self)
        self._hover_clear_timer.setSingleShot(True)
        self._hover_clear_timer.setInterval(100)
        self._hover_clear_timer.timeout.connect(self._do_hover_clear)
        self.thumbnail_view.thumbnailHovered.connect(self._on_thumbnail_hovered)
        self.thumbnail_view.thumbnailLeft.connect(self._on_thumbnail_left)
        self.thumbnail_view.filtersApplied.connect(self._on_filters_applied)
        self._hover_rating_ready.connect(self._on_hover_rating_ready)
        self._hover_metadata_ready.connect(self._on_hover_metadata_ready)
        self._clip_results_ready.connect(self._on_clip_results_ready)

    def _handle_benchmark_result(self, operation: str, time: float):
        logger.info(f"Benchmark - {operation}: {time:.3f} seconds")

    def _is_detail_view_active(self) -> bool:
        current = self.stacked_widget.currentWidget()
        return current is self.picture_view or current is self.video_view or current is self.compare_view

    def _on_thumbnail_hovered(self, path: str):
        if self._is_detail_view_active():
            return
        # Cancel any pending clear — cursor moved to another thumbnail
        self._hover_clear_timer.stop()
        # Publish filepath immediately — no network needed
        if path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="main_window",
                timestamp=time.time(),
                message=path,
                section=StatusSection.FILEPATH,
            ))
        self._hover_prefetch_path = path
        self._hover_prefetch_timer.start()

    def _on_thumbnail_left(self):
        if self._is_detail_view_active():
            return
        # Defer clear: if cursor enters another thumbnail within 100 ms the
        # timer is cancelled in _on_thumbnail_hovered, avoiding flicker.
        self._hover_clear_timer.start()

    def _do_hover_clear(self):
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE, source="main_window",
            timestamp=time.time(), message="", section=StatusSection.FILEPATH))
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE, source="main_window",
            timestamp=time.time(), message="", section=StatusSection.RATING))

    def _fetch_metadata_for_path(self, path: str):
        # why: picture view doesn't trigger the hover-prefetch flow, so we
        # reuse _fetch_hover_rating to populate the cache and notify panels.
        if not path or not self.service:
            return
        threading.Thread(
            target=self._fetch_hover_rating, args=(path,), daemon=True
        ).start()

    def _do_hover_prefetch(self):
        path = self._hover_prefetch_path
        if path:
            self._prefetch_view_image_async(path)
            threading.Thread(
                target=self._fetch_hover_rating, args=(path,), daemon=True
            ).start()

    def _fetch_hover_rating(self, path: str):
        if not self.service:
            return
        try:
            result = self.metadata_cache.fetch_and_cache([path])
            rating = 0
            if path in result:
                rating = result[path].get("rating", 0) or 0
            self._hover_rating_ready.emit(path, int(rating))
            self._hover_metadata_ready.emit(path)
        except Exception as e:  # why: background thread; IPC/socket failure must not crash the worker
            logger.debug(f"Hover rating fetch failed for {path}: {e}")

    def notify_rating_set(self):
        self._last_rating_set_time = time.time()

    def _on_hover_rating_ready(self, path: str, rating: int):
        # Skip stale hover results that were in-flight when a rating was just set
        if time.time() - self._last_rating_set_time < 0.5:
            return
        if self.thumbnail_view.get_hovered_image_path() == path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="main_window",
                timestamp=time.time(),
                message=str(rating),
                section=StatusSection.RATING,
            ))

    def _on_hover_metadata_ready(self, path: str):
        for panel in self.info_panels:
            panel.refresh_if_showing(path)

    def _prefetch_view_image_async(self, path: str):
        if not self.service or not path:
            return
        threading.Thread(
            target=self.service.request_view_image,
            args=(path,),
            daemon=True,
        ).start()

    def _prefetch_neighbors(self, image_path: str):
        files = self.thumbnail_view.current_files
        if not files:
            return
        try:
            idx = files.index(image_path)
        except ValueError:
            return
        n = len(files)
        for neighbor_idx in {(idx - 1) % n, (idx + 1) % n} - {idx}:
            neighbor = files[neighbor_idx]
            if not _is_video(neighbor):
                self._prefetch_view_image_async(neighbor)

    def _open_inspector(self):
        from .inspector_view import InspectorView
        inspector = InspectorView(self.config_manager, inspector_index=self._inspector_slot)
        self._inspector_slot += 1
        inspector.set_service(self.service)
        inspector.set_daemon_signals(self.daemon_signals)
        self.inspector_views.append(inspector)
        inspector.closed.connect(lambda: self._on_inspector_closed(inspector))
        self._tiling.dock_right(inspector)

        if self.picture_view and self.stacked_widget.currentWidget() == self.picture_view:
            self._prime_inspector_from_picture_view(inspector)
        elif self.current_hovered_image:
            event_data = InspectorEventData(
                event_type=EventType.INSPECTOR_UPDATE,
                source="main_window",
                timestamp=time.time(),
                image_path=self.current_hovered_image,
                normalized_position=QPointF(0.5, 0.5),
            )
            inspector.prime(event_data)
        logger.info("Opened tiled Inspector view.")

    def _on_inspector_closed(self, inspector):
        try:
            self.inspector_views.remove(inspector)
        except ValueError:
            return  # already removed by closeEvent teardown loop
        self._tiling.undock(inspector)
        inspector.deleteLater()
        if not self.inspector_views:
            self._inspector_slot = 0

    def _pin_last_inspector(self):
        if self.inspector_views:
            self.inspector_views[-1].toggle_pin()

    # ------------------------------------------------------------------
    #  Face Palette
    # ------------------------------------------------------------------

    def _open_face_palette(self):
        from .face_palette import FacePalette
        palette = FacePalette(config_manager=self.config_manager, parent=self)
        palette.set_service(self.service)
        if self.thumbnail_view:
            palette.set_current_files(list(self.thumbnail_view.all_files))
        palette.restore_selection(self._face_selection)
        self.face_palettes.append(palette)
        palette.closed.connect(lambda: self._on_face_palette_closed(palette))
        palette.show()
        logger.debug("Opened People View window.")

    def _on_face_palette_closed(self, palette):
        self._face_selection = list(palette.get_selection())
        try:
            self.face_palettes.remove(palette)
        except ValueError:
            pass

    def _handle_hotkey_zoom(self, direction: int):
        factor = 1.25 if direction > 0 else 1 / 1.25

        # 1. Focused inspector gets priority
        focused = QApplication.focusWidget()
        if focused:
            for inspector in self.inspector_views:
                if inspector is focused or inspector.isAncestorOf(focused):
                    inspector.zoom_by_factor(factor)
                    return

        # 2. Compare view active → zoom via first split (syncs to all)
        if self.compare_view and self.stacked_widget.currentWidget() is self.compare_view:
            if direction > 0:
                self.compare_view.zoom_in(1.25)
            else:
                self.compare_view.zoom_out(1.25)
            return

        # 3. Picture view active → zoom via its PictureBase
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            if direction > 0:
                self.picture_view.zoom_in(1.25)
            else:
                self.picture_view.zoom_out(1.25)
            return

        # 3. Thumbnail mode → zoom the latest inspector (if any)
        if self.inspector_views:
            self.inspector_views[-1].zoom_by_factor(factor)

    def _open_info_panel(self):
        provider = MetadataProvider(self.metadata_cache)
        panel = InfoPanelShell(provider, self.metadata_cache,
                               panel_index=self._info_panel_slot,
                               config_manager=self.config_manager,
                               parent=self)
        self._info_panel_slot += 1
        self.info_panels.append(panel)
        panel.closed.connect(lambda: self._on_info_panel_closed(panel))
        panel.show()
        logger.info("Opened new Info panel.")

    def _on_info_panel_closed(self, panel):
        try:
            self.info_panels.remove(panel)
        except ValueError:
            return
        if not self.info_panels:
            self._info_panel_slot = 0

    def _toggle_breadcrumb_bar(self):
        if not self.breadcrumb_bar:
            from gui.breadcrumb_bar import BreadcrumbBar
            self.breadcrumb_bar = BreadcrumbBar(service=self.service, parent=self)

        if self.breadcrumb_bar.isVisible():
            self.breadcrumb_bar.hide()
        else:
            path = self.thumbnail_view.current_directory_path if self.thumbnail_view else None
            if path:
                self.breadcrumb_bar.set_path(path)
                self.breadcrumb_bar.show()
                self.breadcrumb_bar.raise_()

    def open_filter_dialog(self):
        if not self.filter_dialog:
            self.filter_dialog = FilterDialog(self)

        if self.filter_dialog.isVisible():
            self.filter_dialog.hide()
            self.filter_dialog.clear_filter()
            event_system.publish(EventData(
                event_type=EventType.CLEAR_FILTERS, source="main_window",
                timestamp=time.time()))
        else:
            self.filter_dialog.show()
            self.filter_dialog.raise_()
            self.filter_dialog.activateWindow()

    def open_tag_filter(self):
        if not self.tag_filter_dialog:
            self.tag_filter_dialog = TagFilterDialog(self)
            self._tag_filter_ready.connect(self._on_tag_filter_ready)

        if self.tag_filter_dialog.isVisible():
            self.tag_filter_dialog.hide()
            self.tag_filter_dialog.clear_filter()
        else:
            self.tag_filter_dialog.show()
            self.tag_filter_dialog.raise_()
            self.tag_filter_dialog.activateWindow()
            if self.thumbnail_view and self.thumbnail_view.service:
                threading.Thread(
                    target=self._fetch_tag_filter_data, daemon=True
                ).start()

    def _fetch_tag_filter_data(self):
        sc = self.thumbnail_view.service
        if not sc:
            return
        try:
            dir_path = self.thumbnail_view.current_directory_path or ""
            tags_resp = sc.get_tags(dir_path)
            if tags_resp:
                dir_tags = [t['name'] for t in tags_resp.get('directory_tags', [])]
                global_tags = [t['name'] for t in tags_resp.get('global_tags', [])]
                self._tag_filter_ready.emit(dir_tags, global_tags)
        except Exception as e:  # why: background IPC; socket failure must not crash the worker
            logger.debug(f"Tag filter fetch failed: {e}")

    @Slot(list, list)
    def _on_tag_filter_ready(self, dir_tags: list, global_tags: list):
        if self.tag_filter_dialog and self.tag_filter_dialog.isVisible():
            self.tag_filter_dialog.set_available_tags(dir_tags, global_tags)

    def get_effective_selection(self) -> list:
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            path = self.picture_view.current_path
            return [path] if path else []

        if self.compare_view and self.stacked_widget.currentWidget() is self.compare_view:
            return self.compare_view.selected_paths

        selected = list(self.selection_state.selected_paths)
        if not selected and self.thumbnail_view:
            hovered = self.thumbnail_view.get_hovered_image_path()
            if hovered:
                return [hovered]
        return selected

    def open_tag_editor(self):
        if not self.thumbnail_view or not self.thumbnail_view.service:
            return
        selected = list(self.script_api.get_selected_images())
        if not selected:
            return

        if not self.tag_editor_dialog:
            self.tag_editor_dialog = TagEditorDialog(self)
            self.tag_editor_dialog.tags_confirmed.connect(self._on_tags_confirmed)
            self._tag_editor_ready.connect(self._on_tag_editor_ready)

        # Capture selection at open time so _on_tags_confirmed uses the
        # correct targets even if the dialog is re-opened before confirm.
        self.tag_editor_dialog.targets = list(selected)
        threading.Thread(
            target=self._fetch_tag_editor_data,
            args=(selected,),
            daemon=True,
        ).start()

    def _fetch_tag_editor_data(self, selected: list):
        sc = self.thumbnail_view.service
        if not sc:
            return
        try:
            dir_path = self.thumbnail_view.current_directory_path or ""
            existing_resp = sc.get_image_tags(selected)
            tags_resp = sc.get_tags(dir_path)

            if existing_resp:
                tag_sets = [set(v) for v in existing_resp.values()]
                common_tags = sorted(set.intersection(*tag_sets)) if tag_sets else []
            else:
                common_tags = []

            dir_tags = [t['name'] for t in tags_resp.get('directory_tags', [])] if tags_resp else []
            global_tags = [t['name'] for t in tags_resp.get('global_tags', [])] if tags_resp else []

            self._tag_editor_ready.emit(len(selected), common_tags, dir_tags, global_tags)
        except Exception as e:  # why: background IPC; socket failure must not crash the worker
            logger.debug(f"Tag editor fetch failed: {e}")

    @Slot(int, list, list, list)
    def _on_tag_editor_ready(self, count: int, common_tags: list, dir_tags: list, global_tags: list):
        if self.tag_editor_dialog:
            self.tag_editor_dialog.open_for_images(count, common_tags, dir_tags, global_tags)

    def _on_tags_confirmed(self, tags_to_add: list, tags_to_remove: list):
        if not self.thumbnail_view or not self.thumbnail_view.service:
            return
        selected = getattr(self.tag_editor_dialog, 'targets', None) or []
        if not selected:
            return
        sc = self.thumbnail_view.service
        if tags_to_add:
            sc.set_tags(selected, tags_to_add)
        if tags_to_remove:
            sc.remove_tags(selected, tags_to_remove)
        # Reapply filters in case tag filter is active
        if self.thumbnail_view.has_active_tag_filter():
            self.thumbnail_view.reapply_filters()

    # ── ComfyUI ─────────────────────────────────────────────────

    def open_comfyui_dialog(self):
        selected = self.get_effective_selection()
        if not selected:
            return

        if not self.comfyui_dialog:
            from .comfyui_dialog import ComfyUIDialog
            self.comfyui_dialog = ComfyUIDialog(self)
            self.comfyui_dialog.generate_requested.connect(self._on_comfyui_generate)
            self.comfyui_dialog.set_daemon_signals(self.daemon_signals)

        self.comfyui_dialog.open_for_images(selected)

    def _on_comfyui_generate(self, image_paths: list, workflow_json: str):
        def _send():
            if not self.service:
                return
            for path in image_paths:
                task_id = self.service.comfyui_generate(path, workflow=workflow_json)
                if task_id:
                    logger.debug(f"ComfyUI generation queued: {task_id}")
                else:
                    logger.warning(f"ComfyUI generate returned no task_id for {path}")

        threading.Thread(target=_send, daemon=True).start()

    # ── CLIP Search ─────────────────────────────────────────────

    def open_clip_search_dialog(self):
        if not self.clip_search_dialog:
            from .clip_search_dialog import ClipSearchDialog
            self.clip_search_dialog = ClipSearchDialog(self)
            self.clip_search_dialog.search_requested.connect(self._on_clip_search)
            self.clip_search_dialog.search_cleared.connect(self._on_clip_search_cleared)
            self.clip_search_dialog.result_selected.connect(self._on_clip_result_selected)
            # Warm the ONNX text session in background so first search is fast
            self._warm_clip_session()

        if self.clip_search_dialog.isVisible():
            self.clip_search_dialog.close()
        else:
            self.clip_search_dialog.show()
            self.clip_search_dialog.activateWindow()

    def _on_clip_search(self, query: str):
        import threading as _threading
        # Save pre-search order so we can restore on clear
        if self.thumbnail_view and self._pre_clip_search_order is None:
            self._pre_clip_search_order = list(self.thumbnail_view.all_files)

        # Snapshot current files on main thread for scoped search
        scope = list(self.thumbnail_view.all_files) if self.thumbnail_view else None

        def _bg_search():
            try:
                if not self.service:
                    self._clip_results_ready.emit(None)
                    return
                results = self.service.clip_search(query, scope=scope)
                self._clip_results_ready.emit(results)
            except Exception:  # why: clip_search calls ONNX inference + DB reads
                logger.error("CLIP search failed", exc_info=True)
                self._clip_results_ready.emit(None)

        _threading.Thread(target=_bg_search, daemon=True).start()

    def _on_clip_results_ready(self, results):
        if not self.clip_search_dialog:
            return
        if results is None:
            self.clip_search_dialog.set_error("Search failed — are models downloaded?")
            return

        self.clip_search_dialog.set_results(results)
        if results and self.thumbnail_view:
            result_paths = [fp for fp, _ in results]
            # Reorder: matched files first (by score), then the rest
            matched_set = set(result_paths)
            rest = [p for p in self.thumbnail_view.all_files if p not in matched_set]
            self.thumbnail_view.reorder_files(result_paths + rest)
            self.thumbnail_view.apply_clip_search_results(result_paths)

    def _on_clip_search_cleared(self):
        if self.thumbnail_view:
            # Restore original order first, then clear the filter.
            # reorder_files rebuilds the layout using current _hidden_indices,
            # so the clip filter must still be active during reorder to avoid
            # a stale layout flash.  clear_clip_search then removes the filter
            # and triggers reapply_filters which unhides everything.
            if self._pre_clip_search_order is not None:
                self.thumbnail_view.reorder_files(self._pre_clip_search_order)
                self._pre_clip_search_order = None
            self.thumbnail_view.clear_clip_search()

    def _on_clip_result_selected(self, file_path: str):
        if self.thumbnail_view:
            self.thumbnail_view.navigate_to_file(file_path)

    def _warm_clip_session(self):
        from core import onnx_runtime
        from core.model_manager import get_model_path
        path = get_model_path("clip-vit-b-32-textual", self.config_manager)
        if path:
            onnx_runtime.prewarm_session(path)

    def _on_filters_applied(self):
        # Case 1: detail view is open — navigate away if current media is now filtered out
        current_path = None
        active_view = None
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            active_view = "picture"
            current_path = self.picture_view.current_path
        elif self.video_view and self.stacked_widget.currentWidget() is self.video_view:
            active_view = "video"
            current_path = self.video_view.current_path

        if active_view and current_path:
            visible = set(self.thumbnail_view.current_files)
            if current_path not in visible:
                if visible:
                    first = self.thumbnail_view.current_files[0]
                    self.open_media_view(first)
                else:
                    if active_view == "picture":
                        self.close_picture_view()
                    else:
                        self.close_video_view()
            return

        # Case 2: thumbnail_view is active — re-emit hover so inspector/status bar refresh
        hovered_path = self.thumbnail_view.get_hovered_image_path()
        if hovered_path:
            self.thumbnail_view.thumbnailHovered.emit(hovered_path)

        # Update face palettes with all files (unfiltered)
        if self.face_palettes and self.thumbnail_view:
            files = list(self.thumbnail_view.all_files)
            for palette in self.face_palettes:
                palette.set_current_files(files)

    def _prime_inspector_from_picture_view(self, inspector):
        if (self.picture_view and self.picture_view.current_path and
                self.picture_view.has_image()):
            try:
                local_pos = self.picture_view.mapFromGlobal(self.picture_view.cursor().pos())
                norm_pos = self.picture_view.screen_to_normalized(QPointF(local_pos))
                norm_pos = QPointF(
                    max(0.0, min(1.0, norm_pos.x())),
                    max(0.0, min(1.0, norm_pos.y())),
                )
                event_data = InspectorEventData(
                    event_type=EventType.INSPECTOR_UPDATE,
                    source="main_window",
                    timestamp=time.time(),
                    image_path=self.picture_view.current_path,
                    normalized_position=norm_pos,
                )
                inspector.prime(event_data)
            except Exception as e:  # why: screenToNormalized may raise before first paint; priming is best-effort
                logger.error(f"Error priming inspector: {e}", exc_info=True)


    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return

        paths = []
        for url in urls:
            p = url.toLocalFile()
            if p:
                paths.append(p)
        if not paths:
            return

        if len(paths) == 1:
            path = paths[0]
            if os.path.isdir(path):  # disk-io: drop path classification
                self.load_directory(path, recursive=False)
            elif os.path.isfile(path):  # disk-io: drop path classification
                self.load_directory(os.path.dirname(path), recursive=False)
                self.open_media_view(path)
        else:
            file_paths = [p for p in paths if os.path.isfile(p)]  # disk-io: drop path filter
            if file_paths:
                self.thumbnail_view.add_images(file_paths)

    def closeEvent(self, event):
        logger.info("GUI close requested.")
        if self.service:
            self.service.prepare_for_shutdown()
        self._hover_clear_timer.stop()
        self._hover_prefetch_timer.stop()

        if self.video_view:
            self.video_view.close()
            self.video_view = None

        if hasattr(self, '_gui_server'):
            self._gui_server.stop()

        # Tear down tiled inspectors
        for inspector in list(self.inspector_views):
            inspector.cleanup()
        self.inspector_views.clear()
        for panel in list(self.info_panels):
            panel.close()
        self.info_panels.clear()
        for palette in list(self.face_palettes):
            palette.close()
        self.face_palettes.clear()
        if self.comfyui_dialog:
            self.comfyui_dialog.close()
            self.comfyui_dialog = None
        self._tiling.save_state()
        settings = QSettings("RabbitViewer", "MainWindow")
        settings.setValue("geometry", self.saveGeometry())
        settings.sync()
        event.accept()
        QApplication.instance().quit()

    def _setup_event_subscriptions(self):
        event_system.subscribe(EventType.INSPECTOR_UPDATE, self._handle_inspector_event)
        # Subscribe to undo/redo events, which might be triggered by menus/etc.
        event_system.subscribe(EventType.UNDO_SELECTION, lambda data: self.selection_history.undo())
        event_system.subscribe(EventType.REDO_SELECTION, lambda data: self.selection_history.redo())
        event_system.subscribe(EventType.OPEN_FILTER, lambda _: self.open_filter_dialog())
        event_system.subscribe(EventType.OPEN_CLIP_SEARCH, lambda _: self.open_clip_search_dialog())
        event_system.subscribe(EventType.OPEN_TAG_EDITOR, lambda _: self.open_tag_editor())
        event_system.subscribe(EventType.OPEN_TAG_FILTER, lambda _: self.open_tag_filter())
        event_system.subscribe(EventType.OPEN_COMPARE_GRID, lambda _: self._open_compare_view("grid"))
        event_system.subscribe(EventType.OPEN_COMPARE_SPLIT, lambda _: self._open_compare_view("split"))
        event_system.subscribe(EventType.NAVIGATE_PARENT, lambda _: self.thumbnail_view.navigate_to_parent())
        event_system.subscribe(EventType.OPEN_BREADCRUMB, lambda _: self._toggle_breadcrumb_bar())
        event_system.subscribe(EventType.NAVIGATE_TO_FOLDER, lambda e: self._handle_folder_navigation(e.path))
        event_system.subscribe(EventType.OPEN_RECENT_DIRECTORY, self._handle_open_recent)

    def _handle_inspector_event(self, event_data):
        if event_data.image_path == self.current_hovered_image:
            return
        self.current_hovered_image = event_data.image_path
        logger.debug("Hovered image updated: %s", self.current_hovered_image)

    def _setup_hotkeys(self):
        hotkeys_config = self.config_manager.get("hotkeys", {})
        self.hotkey_manager = HotkeyManager(self, hotkeys_config)

        self.hotkey_manager.add_action("toggle_inspector", self._open_inspector)
        self.hotkey_manager.add_action("pin_inspector", self._pin_last_inspector)
        self.hotkey_manager.add_action("escape_picture_view", self._close_active_media_view)
        self.hotkey_manager.add_action("close_or_quit", self._handle_close_or_quit)
        self.hotkey_manager.add_action("next_image", lambda: self.navigate_to_image("next"))
        self.hotkey_manager.add_action("previous_image", lambda: self.navigate_to_image("previous"))
        self.hotkey_manager.add_action("undo_selection", self.selection_history.undo)
        self.hotkey_manager.add_action("redo_selection", self.selection_history.redo)
        self.hotkey_manager.add_action("show_hotkey_help", self._toggle_hotkey_help)
        self.hotkey_manager.add_action("toggle_info_panel", self._open_info_panel)
        self.hotkey_manager.add_action("open_comfyui", self.open_comfyui_dialog)
        self.hotkey_manager.add_action("clip_search", self.open_clip_search_dialog)
        self.hotkey_manager.add_action("toggle_face_palette", self._open_face_palette)
        self.hotkey_manager.add_action("zoom_in", lambda: self._handle_hotkey_zoom(1))
        self.hotkey_manager.add_action("zoom_out", lambda: self._handle_hotkey_zoom(-1))
        self.hotkey_manager.add_action("toggle_group_mode", self._toggle_group_mode)
        self.hotkey_manager.add_action("copy_paths", self._copy_paths)
        self.hotkey_manager.add_action("copy_files", self._copy_files)
        self.hotkey_manager.add_action("copy_image", self._copy_image)

    def _toggle_hotkey_help(self):
        if not hasattr(self, '_hotkey_help_overlay') or self._hotkey_help_overlay is None:
            defn = self.hotkey_manager.definitions.get("show_hotkey_help")
            trigger_key = defn.sequences[0] if defn and defn.sequences else "?"
            self._hotkey_help_overlay = HotkeyHelpOverlay(
                self, self.hotkey_manager.definitions, trigger_key)
        self._hotkey_help_overlay.toggle()

    def _toggle_group_mode(self):
        if self.thumbnail_view:
            self.thumbnail_view.toggle_group_mode()

    # ── Clipboard ────────────────────────────────────────────────

    def _copy_paths(self):
        from .clipboard import copy_paths_as_text
        paths = self.get_effective_selection()
        count = copy_paths_as_text(paths)
        if count:
            self._show_status(f"Copied {count} path{'s' if count != 1 else ''}")

    def _copy_files(self):
        from .clipboard import copy_files_to_clipboard
        paths = self.get_effective_selection()
        count = copy_files_to_clipboard(paths)
        if count:
            self._show_status(f"Copied {count} file{'s' if count != 1 else ''}")

    def _copy_image(self):
        from .clipboard import copy_image_pixels, JPEG_EXTENSIONS
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            image = self.picture_view._picture_base.get_image()
            path = self.picture_view.current_path
        else:
            self._show_status("Copy image: open in picture view first")
            return
        ok = copy_image_pixels(image, path)
        if ok:
            self._show_status("Image copied to clipboard")
        elif path:
            ext = os.path.splitext(path)[1].lower()
            if ext not in JPEG_EXTENSIONS:
                self._show_status("Copy image: JPEG only")
            else:
                self._show_status("Copy image: no image loaded")

    def _show_status(self, message: str, timeout: int = 3000):
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="main_window",
            timestamp=time.time(),
            message=message,
            section=StatusSection.PROCESS,
            timeout=timeout,
        ))

    def load_directory(self, directory_path: str, recursive: bool = True):
        logger.info(f"MainWindow: Starting to load directory: {directory_path} (Recursive: {recursive})")
        self.last_known_directory = directory_path
        from core.recent_directories import add as add_recent
        add_recent(directory_path)
        logger.info("MainWindow: Calling thumbnail_view.load_directory...")
        self.thumbnail_view.load_directory(directory_path, recursive)
        logger.info("MainWindow: Directory loading completed, setting current widget...")
        self.stacked_widget.setCurrentWidget(self.thumbnail_view)
        logger.info("MainWindow: ThumbnailView is now the current widget")

    def _handle_open_recent(self, event_data):
        self.load_directory(event_data.path)

    def get_removed_images(self) -> Set[str]:
        return set(self._removed_images)

    def remove_images(self, image_paths: List[str]):
        if not self.thumbnail_view:
            return
        removed_set = set(image_paths)

        # Determine if the active media view is showing a removed image.
        current_path = None
        active_view = None
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            active_view = "picture"
            current_path = self.picture_view.current_path
        elif self.video_view and self.stacked_widget.currentWidget() is self.video_view:
            active_view = "video"
            current_path = self.video_view.current_path

        self.thumbnail_view.remove_images(image_paths)
        self._removed_images = image_paths.copy()

        # If the active media view was showing a removed image, navigate or close.
        if active_view and current_path and current_path in removed_set:
            visible = self.thumbnail_view.current_files
            if visible:
                self.open_media_view(visible[0])
            elif active_view == "picture":
                self.close_picture_view()
            else:
                self.close_video_view()


    @Slot()
    def _handle_thumbnail_double_click(self):
        target_image = self.current_hovered_image
        if not target_image:
            return
        self.open_media_view(target_image)

    @Slot(str)
    def _handle_folder_navigation(self, folder_path: str):
        self.last_known_directory = folder_path
        self.thumbnail_view.navigate_to_folder(folder_path)
        if self.breadcrumb_bar and self.breadcrumb_bar.isVisible():
            self.breadcrumb_bar.set_path(folder_path)

    def open_media_view(self, file_path: str):
        if _is_video(file_path):
            self._open_video_view(file_path)
        else:
            self._open_picture_view(file_path)

    def _open_video_view(self, video_path: str):
        if self.picture_view:
            self.picture_view.close()
            self.picture_view = None
        try:
            if not self.video_view:
                from gui.video_view import VideoView
                self.video_view = VideoView()
                self.video_view.escapePressed.connect(self.close_video_view)
                self.video_view.navigateRequested.connect(self.navigate_to_image)
                self.video_view.set_service(self.service)
                self.stacked_widget.addWidget(self.video_view)
            self.video_view.loadVideo(video_path)
            self._hover_clear_timer.stop()
            self.stacked_widget.setCurrentWidget(self.video_view)
            self.video_view.setFocus()
        except Exception as e:  # why: python-mpv init may raise on missing libmpv or unsupported format
            logger.error(f"Failed to open video view: {e}", exc_info=True)

    def _open_picture_view(self, image_path: str):
        if self.video_view:
            self.video_view.close()
            self.video_view = None
        try:
            if not self.picture_view:
                from .picture_view import PictureView
                self.picture_view = PictureView()
                self.picture_view.escapePressed.connect(self.close_picture_view)
                self.picture_view.set_service(self.service)
                self.picture_view.set_daemon_signals(self.daemon_signals)
                self.stacked_widget.addWidget(self.picture_view)
            self.picture_view.loadImage(image_path)
            self._prefetch_neighbors(image_path)
            self._hover_clear_timer.stop()
            self.stacked_widget.setCurrentWidget(self.picture_view)
            self.picture_view.setFocus()
            self._fetch_metadata_for_path(image_path)
        except Exception as e:  # why: loadImage delegates to format plugins which may raise arbitrarily
            logger.error(f"Exception when opening Picture View: {e}", exc_info=True)

    def _handle_close_or_quit(self):
        if self.compare_view and self.stacked_widget.currentWidget() is self.compare_view:
            self.close_compare_view()
        elif self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            self.close_picture_view()
        elif self.video_view and self.stacked_widget.currentWidget() is self.video_view:
            self.close_video_view()
        elif self.inspector_views:
            self.inspector_views[-1].cleanup()
        elif self.info_panels:
            self.info_panels[-1].close()
        elif self.face_palettes:
            self.face_palettes[-1].close()
        else:
            self.close()

    def _close_active_media_view(self):
        if self.compare_view and self.stacked_widget.currentWidget() is self.compare_view:
            self.close_compare_view()
        elif self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            self.close_picture_view()
        elif self.video_view and self.stacked_widget.currentWidget() is self.video_view:
            self.close_video_view()

    def close_picture_view(self):
        logger.debug("Closing picture view")
        try:
            if self.picture_view:
                current_path = self.picture_view.current_path
                self.stacked_widget.setCurrentWidget(self.thumbnail_view)

                if current_path:
                    self.thumbnail_view.scroll_to_top(current_path)

                # close() triggers closeEvent (unsubscribes events) then deleteLater() via WA_DeleteOnClose.
                self.picture_view.close()
                self.picture_view = None
        except RuntimeError as e:
            logger.error(f"Error closing picture view: {e}", exc_info=True)

    def close_video_view(self):
        logger.debug("Closing video view")
        try:
            if self.video_view:
                current_path = self.video_view.current_path
                self.stacked_widget.setCurrentWidget(self.thumbnail_view)
                if current_path:
                    self.thumbnail_view.setHighlightedThumbnail(current_path)
                self.video_view.close()
                self.video_view = None
        except RuntimeError as e:
            logger.error(f"Error closing video view: {e}", exc_info=True)

    def _open_compare_view(self, mode: str = "split"):
        selected = list(self.selection_state.selected_paths)
        if len(selected) < 2:
            return
        self._close_active_media_view()
        try:
            if not self.compare_view:
                from .compare_view import CompareView
                self.compare_view = CompareView()
                self.compare_view.escapePressed.connect(self.close_compare_view)
                self.compare_view.set_service(self.service)
                self.stacked_widget.addWidget(self.compare_view)
            self.compare_view.load_images(selected, mode=mode)
            self._hover_clear_timer.stop()
            self.stacked_widget.setCurrentWidget(self.compare_view)
            self.compare_view.setFocus()
        except Exception as e:  # why: image loading may raise from plugins or socket errors
            logger.error(f"Exception when opening Compare View: {e}", exc_info=True)

    def close_compare_view(self):
        logger.debug("Closing compare view")
        try:
            if self.compare_view:
                self.stacked_widget.setCurrentWidget(self.thumbnail_view)
                self.compare_view.close()
                self.compare_view = None
        except RuntimeError as e:
            logger.error(f"Error closing compare view: {e}", exc_info=True)

    def navigate_to_image(self, direction: str):
        current_path = None
        if self.picture_view and self.stacked_widget.currentWidget() is self.picture_view:
            current_path = self.picture_view.current_path
        elif self.video_view and self.stacked_widget.currentWidget() is self.video_view:
            current_path = self.video_view.current_path

        if not current_path:
            return

        try:
            current_idx = self.thumbnail_view.current_files.index(current_path)
        except ValueError:
            logger.warning(f"Current media {current_path} not found in visible files")
            return
        num_visible = len(self.thumbnail_view.current_files)
        if num_visible == 0:
            return
        if direction == "next":
            new_idx = (current_idx + 1) % num_visible
        elif direction == "previous":
            new_idx = (current_idx - 1 + num_visible) % num_visible
        else:
            return
        new_path = self.thumbnail_view.current_files[new_idx]
        self.open_media_view(new_path)
