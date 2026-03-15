"""Crops load asynchronously on a background thread so the window opens instantly."""
import logging
import os
import random
import time
import threading
from typing import Optional, List, Dict

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea, QLabel,
    QMenu, QFrame, QInputDialog,
)
from PySide6.QtCore import Qt, Signal, QSettings, QEvent
from PySide6.QtGui import QPixmap, QImage

from core.event_system import event_system, EventType, FacePersonFilterEventData
from gui.components.item_card import ItemCard

logger = logging.getLogger(__name__)

_BG = "#1e1e1e"
_TEXT = "#dcdcdc"
_TEXT_DIM = "#888888"
_BORDER = "#2a2a2a"
_CARD_SIZE = 100
_CROP_SIZE = 76
_SECTION_HEADER_HEIGHT = 20


class PersonCard(ItemCard):

    def __init__(self, person_id: str, name: str, config: dict = None, parent=None):
        super().__init__(_CARD_SIZE, config or {}, parent)
        self.person_id = person_id
        self._name = name
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)
        layout.setAlignment(Qt.AlignCenter)

        self._crop_label = QLabel()
        self._crop_label.setFixedSize(_CROP_SIZE, _CROP_SIZE)
        self._crop_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._crop_label, alignment=Qt.AlignCenter)

        display_name = name or "Unnamed"
        if len(display_name) > 10:
            display_name = display_name[:9] + "\u2026"
        self._name_label = QLabel(display_name)
        self._name_label.setAlignment(Qt.AlignCenter)
        self._name_label.setStyleSheet(f"color: {_TEXT}; font-size: 10px;")
        layout.addWidget(self._name_label)

    def _style_selector(self) -> str:
        return "PersonCard"

    def setCrop(self, pixmap: QPixmap):
        if not pixmap.isNull():
            self._crop_label.setPixmap(pixmap.scaled(
                _CROP_SIZE, _CROP_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class FacePalette(QWidget):

    closed = Signal()
    _crop_ready = Signal(str, QImage)  # (person_id, crop_image) — internal, from bg thread

    def __init__(self, config_manager=None, parent=None):
        super().__init__(parent, Qt.Tool)
        self._config_manager = config_manager
        self._service = None
        self._crop_generation = 0  # incremented on refresh to drop stale crops
        self._current_files: Optional[List[str]] = None  # scoped to thumbnail view
        self._gui_config = config_manager.get("gui", {}) if config_manager else {}

        # Selection state
        self._selected_person_ids: List[str] = []
        self._person_cards: Dict[str, PersonCard] = {}  # person_id -> card

        self.setWindowTitle("People")
        self.setMinimumSize(300, 250)

        settings = QSettings("RabbitViewer", "FacePalette")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(420, 500)

        self._build_ui()
        self._crop_ready.connect(self._on_crop_ready, Qt.QueuedConnection)

        self._scroll.viewport().installEventFilter(self)
        self._grid_container.installEventFilter(self)

    def set_service(self, service):
        self._service = service
        self._refresh_grid()

    def set_current_files(self, file_paths: List[str]):
        self._current_files = file_paths
        self._refresh_grid()

    def get_selection(self) -> List[str]:
        return list(self._selected_person_ids)

    def restore_selection(self, person_ids: List[str]):
        """Restore a previously saved selection, dropping IDs that no longer exist."""
        self._selected_person_ids = [
            pid for pid in person_ids if pid in self._person_cards
        ]
        self._sync_selection_visuals()
        if self._selected_person_ids:
            self._emit_filter()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.setStyleSheet(f"""
            FacePalette {{
                background: {_BG};
                border: 1px solid {_BORDER};
            }}
        """)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")

        self._grid_container = QWidget()
        self._scroll.setWidget(self._grid_container)
        main_layout.addWidget(self._scroll, 1)

        self._named_header = QLabel("Named", self._grid_container)
        self._named_header.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; padding: 2px;")
        self._unnamed_header = QLabel("Unnamed", self._grid_container)
        self._unnamed_header.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; padding: 2px;")

        self._empty_label = QLabel("No faces detected yet", self._grid_container)
        self._empty_label.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; padding: 16px;")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setVisible(False)

        self._named_ids: List[str] = []
        self._unnamed_ids: List[str] = []

    # ------------------------------------------------------------------
    #  Grid population
    # ------------------------------------------------------------------

    def _refresh_grid(self):
        for card in self._person_cards.values():
            card.deleteLater()
        self._person_cards.clear()
        self._named_ids.clear()
        self._unnamed_ids.clear()
        self._crop_generation += 1

        if not self._service:
            self._named_header.setVisible(False)
            self._unnamed_header.setVisible(False)
            self._empty_label.setVisible(True)
            return

        if self._current_files is not None:
            persons = self._service.get_persons_for_files(self._current_files)
        else:
            persons = self._service.get_all_persons()
        if not persons:
            self._named_header.setVisible(False)
            self._unnamed_header.setVisible(False)
            self._empty_label.setVisible(True)
            return

        self._empty_label.setVisible(False)

        named = sorted([p for p in persons if p['name']], key=lambda p: p['name'].lower())
        unnamed = sorted([p for p in persons if not p['name'] and p['face_count'] >= 2],
                         key=lambda p: p['face_count'], reverse=True)

        self._named_header.setVisible(bool(named))
        self._unnamed_header.setVisible(bool(unnamed))

        for person in named:
            self._create_card(person)
            self._named_ids.append(person['person_id'])

        for person in unnamed:
            self._create_card(person)
            self._unnamed_ids.append(person['person_id'])

        self._layout_cards()
        self._start_crop_loader(persons)

    def _create_card(self, person: dict) -> PersonCard:
        card = PersonCard(
            person['person_id'], person['name'],
            config=self._gui_config, parent=self._grid_container)
        card.setSelected(person['person_id'] in self._selected_person_ids)
        self._person_cards[person['person_id']] = card
        card.show()
        return card

    # ------------------------------------------------------------------
    #  Absolute positioning layout
    # ------------------------------------------------------------------

    def _layout_cards(self):
        """Reposition cards using the same column/centering math as VirtualGridManager."""
        spacing = self._gui_config.get("spacing", 5)
        available = self._scroll.viewport().width()
        cell = _CARD_SIZE + spacing
        content_width = available - 2 * spacing
        cols = max(1, int((content_width + spacing) / cell)) if cell > 0 else 1

        # Center the grid horizontally (mirrors VirtualGridManager._x_offset).
        grid_width = spacing + cols * cell
        x_offset = max(0, (available - grid_width) // 2)

        y = spacing

        def _place_section(ids: list, y_start: int) -> int:
            y = y_start
            for i, pid in enumerate(ids):
                card = self._person_cards.get(pid)
                if not card:
                    continue
                col = i % cols
                row = i // cols
                x = x_offset + spacing + col * cell
                card.move(x, y + row * cell)
            if ids:
                rows = (len(ids) + cols - 1) // cols
                y += rows * cell
            return y

        if self._named_ids:
            self._named_header.move(x_offset + spacing, y)
            self._named_header.resize(content_width, _SECTION_HEADER_HEIGHT)
            y += _SECTION_HEADER_HEIGHT
            y = _place_section(self._named_ids, y)
            y += spacing

        if self._unnamed_ids:
            self._unnamed_header.move(x_offset + spacing, y)
            self._unnamed_header.resize(content_width, _SECTION_HEADER_HEIGHT)
            y += _SECTION_HEADER_HEIGHT
            y = _place_section(self._unnamed_ids, y)

        self._grid_container.setFixedHeight(y + spacing)

    # ------------------------------------------------------------------
    #  Crop cache
    # ------------------------------------------------------------------

    @staticmethod
    def _crop_cache_dir() -> str:
        d = os.path.expanduser("~/.rabbitviewer/face_crops")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _crop_cache_path(person_id: str) -> str:
        return os.path.join(
            os.path.expanduser("~/.rabbitviewer/face_crops"),
            f"{person_id}.jpg")

    def _invalidate_crop_cache(self, *person_ids: str):
        for pid in person_ids:
            path = self._crop_cache_path(pid)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------
    #  Async crop loading
    # ------------------------------------------------------------------

    def _start_crop_loader(self, persons: list):
        generation = self._crop_generation
        person_feature_map = {
            p['person_id']: p.get('feature_face_id')
            for p in persons
        }
        service = self._service
        thread = threading.Thread(
            target=self._crop_loader_thread,
            args=(person_feature_map, service, generation),
            daemon=True,
        )
        thread.start()

    def _crop_loader_thread(self, person_feature_map, service, generation):
        cache_dir = self._crop_cache_dir()
        pids_needing_gen = []

        for pid in person_feature_map:
            if generation != self._crop_generation:
                return
            cached = os.path.join(cache_dir, f"{pid}.jpg")
            if os.path.isfile(cached):  # disk-io: face crop cache check
                qimg = QImage(cached)
                if not qimg.isNull():
                    self._crop_ready.emit(pid, qimg)
                    continue
            pids_needing_gen.append(pid)

        if not pids_needing_gen or generation != self._crop_generation:
            return

        subset = {pid: person_feature_map[pid] for pid in pids_needing_gen}
        face_map = service.get_feature_faces_batch(subset)

        try:
            from PIL import Image as PILImage
        except ImportError:
            logger.warning("PIL not available for face crop loading")
            return

        for pid in pids_needing_gen:
            if generation != self._crop_generation:
                return
            face = face_map.get(pid)
            if not face:
                continue
            try:
                self._generate_crop(pid, face, service, PILImage, cache_dir)
            except Exception:  # why: PIL open/resize/crop can raise OSError, UnidentifiedImageError, or struct.error
                logger.debug("Failed to load crop for person %s", pid, exc_info=True)

    def _generate_crop(self, pid, face, service, PILImage, cache_dir):
        fp = face['file_path']
        bbox = face['bbox']

        statuses = service.get_previews_status([fp])
        status = statuses.get(fp, {})
        source = (status.get('view_image_path')
                  or status.get('thumbnail_path')
                  or fp)

        if not os.path.isfile(source):  # disk-io: face crop source check
            return

        img = PILImage.open(source)
        img = img.convert("RGB")

        w, h = img.size
        bbox_frac = max(bbox[2], bbox[3], 0.01)
        needed = int(_CROP_SIZE * 2 / bbox_frac)
        if max(w, h) > needed:
            scale = needed / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), PILImage.BILINEAR)
            w, h = new_w, new_h

        x = int(bbox[0] * w)
        y = int(bbox[1] * h)
        bw = int(bbox[2] * w)
        bh = int(bbox[3] * h)

        pad = int(max(bw, bh) * 0.15)
        x = max(0, x - pad)
        y = max(0, y - pad)
        bw = min(w - x, bw + 2 * pad)
        bh = min(h - y, bh + 2 * pad)

        crop = img.crop((x, y, x + bw, y + bh))
        cache_path = os.path.join(cache_dir, f"{pid}.jpg")
        crop.save(cache_path, "JPEG", quality=85)

        data = crop.tobytes("raw", "RGB")
        qimg = QImage(data, crop.width, crop.height,
                      crop.width * 3, QImage.Format.Format_RGB888).copy()
        self._crop_ready.emit(pid, qimg)

    def _on_crop_ready(self, person_id: str, crop_image: QImage):
        card = self._person_cards.get(person_id)
        if card:
            card.setCrop(QPixmap.fromImage(crop_image))

    # ------------------------------------------------------------------
    #  Selection (click / ctrl+click / shift+click)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        card = self._card_at_pos(event.pos())
        if not card:
            if self._selected_person_ids:
                self._clear_selection()
            return

        pid = card.person_id
        modifiers = event.modifiers()
        selected_set = set(self._selected_person_ids)

        if modifiers & Qt.ShiftModifier and modifiers & Qt.ControlModifier:
            # Shift+Ctrl: replace (same as plain click)
            if pid in selected_set:
                self._selected_person_ids = []
            else:
                self._selected_person_ids = [pid]
        elif modifiers & Qt.ControlModifier:
            # Ctrl+click: remove only (no-op if not selected)
            if pid in selected_set:
                self._selected_person_ids.remove(pid)
        elif modifiers & Qt.ShiftModifier:
            # Shift+click: add single item to selection
            if pid not in selected_set:
                self._selected_person_ids.append(pid)
        else:
            # Plain click: replace (clear all if clicked is already selected)
            if pid in selected_set:
                self._selected_person_ids = []
            else:
                self._selected_person_ids = [pid]

        self._sync_selection_visuals()
        self._emit_filter()

    def eventFilter(self, obj, event):
        """Catch clicks on empty space inside the scroll area."""
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            local = self.mapFromGlobal(obj.mapToGlobal(event.pos()))
            card = self._card_at_pos(local)
            if not card and self._selected_person_ids:
                self._clear_selection()
                return True
        return super().eventFilter(obj, event)

    def _card_at_pos(self, pos) -> Optional[PersonCard]:
        widget = self.childAt(pos)
        while widget:
            if isinstance(widget, PersonCard):
                return widget
            widget = widget.parentWidget()
            if widget is self:
                break
        return None

    def _sync_selection_visuals(self):
        for pid, card in self._person_cards.items():
            card.setSelected(pid in self._selected_person_ids)

    def _clear_selection(self):
        self._selected_person_ids.clear()
        self._sync_selection_visuals()
        self._emit_filter()

    def _emit_filter(self):
        ids = list(self._selected_person_ids)
        event_system.publish(FacePersonFilterEventData(
            event_type=EventType.FACE_PERSON_FILTER,
            source="face_palette",
            timestamp=time.time(),
            person_ids=ids,
        ))

    # ------------------------------------------------------------------
    #  Context menu
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event):
        card = self._card_at_pos(event.pos())
        if not card:
            return

        pid = card.person_id
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {_BG}; color: {_TEXT}; border: 1px solid {_BORDER};
            }}
            QMenu::item:selected {{ background: #444; }}
        """)

        rename_action = menu.addAction("Rename")
        feature_action = menu.addAction("Set as Feature Face")
        shuffle_action = menu.addAction("Shuffle Preview")
        menu.addSeparator()

        merge_action = None
        if len(self._selected_person_ids) > 1 and pid in self._selected_person_ids:
            merge_action = menu.addAction(
                f"Merge {len(self._selected_person_ids)} people")

        ungroup_action = menu.addAction("Ungroup")
        hide_action = menu.addAction("Hide")

        action = menu.exec(event.globalPos())
        if not action or not self._service:
            return

        if action == rename_action:
            self._rename_person(pid)
        elif action == feature_action:
            self._set_feature_face_dialog(pid)
        elif action == shuffle_action:
            self._shuffle_preview(pid)
        elif action == merge_action:
            self._merge_selected(pid)
        elif action == ungroup_action:
            self._service.ungroup_person(pid)
            self._invalidate_crop_cache(pid)
            if pid in self._selected_person_ids:
                self._selected_person_ids.remove(pid)
            self._refresh_grid()
            self._sync_selection_visuals()
            self._emit_filter()
        elif action == hide_action:
            self._service.hide_person(pid, True)
            if pid in self._selected_person_ids:
                self._selected_person_ids.remove(pid)
            self._refresh_grid()
            self._sync_selection_visuals()
            self._emit_filter()

    def _rename_person(self, person_id: str):
        if not self._service:
            return
        current_name = ''
        card = self._person_cards.get(person_id)
        if card:
            current_name = card._name
        name, ok = QInputDialog.getText(self, "Rename Person", "Name:", text=current_name)
        if ok and name.strip():
            self._service.rename_person(person_id, name.strip())
            self._refresh_grid()
            self._sync_selection_visuals()

    def _set_feature_face_dialog(self, person_id: str):
        if not self._service:
            return
        faces = self._service.get_faces_for_person(person_id)
        if faces:
            self._service.set_feature_face(person_id, faces[0]['face_id'])
            self._invalidate_crop_cache(person_id)
            self._refresh_grid()
            self._sync_selection_visuals()

    def _shuffle_preview(self, person_id: str):
        if not self._service:
            return
        faces = self._service.get_faces_for_person(person_id)
        if not faces:
            return
        try:
            from core.face_inference import bytes_to_embedding
            from core.face_clustering import compute_person_mean
            embeddings = [bytes_to_embedding(f['embedding']) for f in faces]
            mean = compute_person_mean(embeddings)
            if mean is not None:
                scores = [float(mean @ emb) for emb in embeddings]
                top = max(scores)
                candidates = [f for f, s in zip(faces, scores) if s >= top * 0.9]
            else:
                candidates = faces
        except Exception:
            candidates = faces
        chosen = random.choice(candidates)
        self._service.set_feature_face(person_id, chosen['face_id'])
        self._invalidate_crop_cache(person_id)
        self._refresh_grid()
        self._sync_selection_visuals()

    def _merge_selected(self, target_id: str):
        if not self._service:
            return
        source_ids = [pid for pid in self._selected_person_ids if pid != target_id]
        if not source_ids:
            return
        self._invalidate_crop_cache(target_id, *source_ids)
        self._service.merge_persons(target_id, source_ids)
        self._selected_person_ids = [target_id]
        self._refresh_grid()
        self._sync_selection_visuals()
        self._emit_filter()

    # ------------------------------------------------------------------
    #  Resize / lifecycle
    # ------------------------------------------------------------------

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._person_cards:
            self._layout_cards()

    def closeEvent(self, event):
        self._crop_generation += 1
        settings = QSettings("RabbitViewer", "FacePalette")
        settings.setValue("geometry", self.saveGeometry())
        settings.sync()
        self.closed.emit()
        super().closeEvent(event)
