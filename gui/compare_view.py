import math
import logging
logger = logging.getLogger(__name__)
import threading
import time

from PySide6.QtWidgets import QWidget, QGridLayout
from PySide6.QtCore import Qt, Signal, QPointF, QSizeF, QPoint
from PySide6.QtGui import QPainter, QImage, QMouseEvent, QPaintEvent, QResizeEvent, QWheelEvent, QKeyEvent

from core.event_system import EventType, StatusMessageEventData, StatusSection, event_system
from .picture_base import PictureBase, ViewState


class _CompareSplit(QWidget):

    _image_ready = Signal(QImage)  # marshals loaded image to GUI thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self.update)
        self._current_path = None
        self._service = None
        self._is_panning = False
        self._last_mouse_pos = QPoint()
        self._interacting = False
        self._user_navigating = False
        self._image_ready.connect(self._on_image_ready)

        # Offset from master: client_pos = master_pos + center_offset
        self.center_offset = QPointF(0.0, 0.0)
        self.zoom_ratio = 1.0  # client_zoom = master_zoom * zoom_ratio

    def fetch_image(self, image_path: str, service):
        if not service:
            return
        self._service = service
        result = service.request_view_image(image_path)
        if result is None:
            return
        # why: QImage construction + load is thread-safe (no GUI dependency);
        # _image_ready signal marshals to GUI thread before any widget mutation.
        image = QImage()
        if result.get('view_image_source') == "memory":
            image_bytes = service.get_cached_view_image(image_path)
            if image_bytes:
                image.loadFromData(image_bytes)
        elif result.get('view_image_path'):
            image.load(result['view_image_path'])
        if not image.isNull():
            self._current_path = image_path
            self._image_ready.emit(image)

    def _on_image_ready(self, image: QImage):
        self._picture_base.setImage(image)
        self._apply_orientation()
        self._picture_base.setFitMode(True)
        self.update()

    def _apply_orientation(self) -> None:
        if not self._service or not self._current_path:
            return
        try:
            resp = self._service.get_metadata_batch([self._current_path])
            orientation = resp.get(self._current_path, {}).get('orientation', 1) or 1
        except Exception:  # why: service unavailable or NAS drop; orientation is best-effort
            return
        self._picture_base.setOrientationFromExif(orientation)

    def apply_view_state(self, center: QPointF, zoom: float, fit_mode: bool):
        if fit_mode:
            self._picture_base.setFitMode(True)
        else:
            self._picture_base.setZoom(zoom, center)

    def view_state(self) -> ViewState:
        return self._picture_base.viewState()

    @property
    def interacting(self) -> bool:
        return self._interacting

    @interacting.setter
    def interacting(self, value: bool):
        self._interacting = value

    @property
    def user_navigating(self) -> bool:
        return self._user_navigating

    def zoom_in(self, factor: float = 1.25):
        self._user_navigating = True
        self._picture_base.zoomIn(factor)
        self._user_navigating = False

    def zoom_out(self, factor: float = 1.25):
        self._user_navigating = True
        self._picture_base.zoomOut(factor)
        self._user_navigating = False

    def cleanup(self):
        self._picture_base.cleanup_subscriptions()

    # -- Qt event overrides --------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._picture_base.has_image():
            return
        painter = QPainter(self)
        if not self._interacting:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setTransform(self._picture_base.calculateTransform())
        painter.drawImage(0, 0, self._picture_base.get_image())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._interacting = True
            self._user_navigating = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
            self._interacting = True
            self._user_navigating = True
            anchor = self._picture_base.screenToNormalized(QPointF(event.position()))
            self._picture_base.startDragZoom(anchor, QPointF(event.position()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_panning:
            delta = event.position().toPoint() - self._last_mouse_pos
            self._last_mouse_pos = event.position().toPoint()
            transform = self._picture_base.calculateTransform()
            inv_transform, invertible = transform.inverted()
            if invertible:
                delta_normalized = inv_transform.map(QPointF(delta)) - inv_transform.map(QPointF(0, 0))
                current_center = self._picture_base.viewState().center
                new_center = QPointF(
                    current_center.x() - delta_normalized.x() / self._picture_base.paddedRect().width(),
                    current_center.y() + delta_normalized.y() / self._picture_base.paddedRect().height(),
                )
                self._picture_base.setCenter(new_center)
        elif self._picture_base.isDragZooming():
            self._picture_base.updateDragZoom(QPointF(event.position()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = False
            self._end_interaction()
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()
            self._end_interaction()

    def _end_interaction(self):
        self._interacting = False
        self._user_navigating = False
        self.update()
        parent = self.parent()
        if isinstance(parent, CompareView):
            parent._finish_interaction(self)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._user_navigating = True
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
                self._picture_base.setZoom(1.0, click_pos)
            else:
                self._picture_base.setFitMode(True)
            self._user_navigating = False

    def enterEvent(self, event) -> None:
        if self._current_path:
            event_system.publish(StatusMessageEventData(
                event_type=EventType.STATUS_MESSAGE,
                source="compare_view",
                timestamp=time.time(),
                message=self._current_path,
                section=StatusSection.FILEPATH,
            ))

    def leaveEvent(self, event) -> None:
        event_system.publish(StatusMessageEventData(
            event_type=EventType.STATUS_MESSAGE,
            source="compare_view",
            timestamp=time.time(),
            message="",
            section=StatusSection.FILEPATH,
        ))

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._user_navigating = True
        center = self._picture_base.screenToNormalized(QPointF(event.position()))
        factor = 1.25
        if delta > 0:
            self._picture_base.zoomIn(factor, center)
        else:
            self._picture_base.zoomOut(factor, center)
        self._user_navigating = False


class CompareView(QWidget):
    escapePressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self._grid_layout = QGridLayout(self)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(2)
        self._splits: list[_CompareSplit] = []
        self._syncing = False
        self.service = None

    def set_service(self, service):
        self.service = service

    def load_images(self, image_paths: list[str]):
        for split in self._splits:
            split._picture_base.viewStateChanged.disconnect()
            split.cleanup()
            self._grid_layout.removeWidget(split)
            split.deleteLater()
        self._splits.clear()

        n = len(image_paths)
        if n == 0:
            return

        cols = math.ceil(math.sqrt(n))

        for i, path in enumerate(image_paths):
            split = _CompareSplit(self)
            split._picture_base.viewStateChanged.connect(
                lambda state, s=split: self._on_split_navigated(s, state)
            )
            self._splits.append(split)
            self._grid_layout.addWidget(split, i // cols, i % cols)

        for split, path in zip(self._splits, image_paths):
            threading.Thread(
                target=self._load_split_image, args=(split, path), daemon=True
            ).start()

    def _load_split_image(self, split: _CompareSplit, path: str):
        try:
            split.fetch_image(path, self.service)
        except Exception as e:  # why: service/plugin errors must not crash the worker thread
            logger.error(f"CompareView: failed to load {path}: {e}", exc_info=True)

    @property
    def _master(self) -> _CompareSplit | None:
        return self._splits[0] if self._splits else None

    def _on_split_navigated(self, sender: _CompareSplit, state: ViewState):
        if self._syncing or not sender.user_navigating:
            return
        self._syncing = True

        if sender is self._master:
            for split in self._splits[1:]:
                split.interacting = sender.interacting
                split.apply_view_state(
                    QPointF(state.center.x() + split.center_offset.x(),
                            state.center.y() + split.center_offset.y()),
                    state.zoom * split.zoom_ratio,
                    state.fit_mode,
                )
        else:
            # Client navigated by user → update its offset relative to master
            master = self._master
            if master:
                master_state = master.view_state()
                sender.center_offset = QPointF(
                    state.center.x() - master_state.center.x(),
                    state.center.y() - master_state.center.y(),
                )
                if master_state.zoom > 0:
                    sender.zoom_ratio = state.zoom / master_state.zoom

        self._syncing = False

    def _finish_interaction(self, source: _CompareSplit):
        for split in self._splits:
            if split is not source:
                split.interacting = False
                split.update()

    def zoom_in(self, factor: float = 1.25):
        if self._splits:
            self._splits[0].zoom_in(factor)

    def zoom_out(self, factor: float = 1.25):
        if self._splits:
            self._splits[0].zoom_out(factor)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()

    def closeEvent(self, event):
        for split in self._splits:
            split.cleanup()
        super().closeEvent(event)
