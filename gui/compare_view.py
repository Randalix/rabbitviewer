import math
import logging
import threading

from PySide6.QtWidgets import QWidget, QGridLayout
from PySide6.QtCore import Qt, Signal, QPointF, QSizeF, QPoint
from PySide6.QtGui import QPainter, QMouseEvent, QPaintEvent, QResizeEvent, QWheelEvent, QKeyEvent

from .picture_base import PictureBase, ViewState


class _CompareSplit(QWidget):
    """A single image panel within the compare grid."""

    viewSynced = Signal(object)  # emits ViewState when user navigates

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._picture_base = PictureBase()
        self._picture_base.viewStateChanged.connect(self._on_view_state_changed)
        self._current_path = None
        self._is_panning = False
        self._last_mouse_pos = QPoint()
        self._syncing = False

    def load_image(self, image_path: str, socket_client) -> bool:
        if not socket_client:
            return False
        result = socket_client.request_view_image(image_path)
        if result is None:
            return False
        if result.get('view_image_source') == "memory":
            image_bytes = socket_client.get_cached_view_image(image_path)
            success = self._picture_base.loadImageFromBytes(image_bytes) if image_bytes else False
        elif result.get('view_image_path'):
            success = self._picture_base.loadImageFromPath(result['view_image_path'])
        else:
            return False
        if success:
            self._current_path = image_path
            self._picture_base.setFitMode(True)
        return success

    def apply_view_state(self, center: QPointF, zoom: float, fit_mode: bool):
        self._syncing = True
        if fit_mode:
            self._picture_base.setFitMode(True)
        else:
            self._picture_base.setZoom(zoom, center)
        self._syncing = False

    def _on_view_state_changed(self, state: ViewState):
        if not self._syncing:
            self.viewSynced.emit(state)

    def zoom_in(self, factor: float = 1.25):
        self._picture_base.zoomIn(factor)

    def zoom_out(self, factor: float = 1.25):
        self._picture_base.zoomOut(factor)

    def cleanup(self):
        self._picture_base.cleanup_subscriptions()

    # -- Qt event overrides --------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._picture_base.has_image():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setTransform(self._picture_base.calculateTransform())
        painter.drawImage(0, 0, self._picture_base.get_image())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._picture_base.setViewportSize(QSizeF(event.size()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._is_panning = True
            self._last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
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
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.RightButton:
            self._picture_base.endDragZoom()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self._picture_base.isFitMode() or abs(self._picture_base.viewState().zoom - 1.0) > 0.01:
                click_pos = self._picture_base.screenToNormalized(QPointF(event.position()))
                self._picture_base.setZoom(1.0, click_pos)
            else:
                self._picture_base.setFitMode(True)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        center = self._picture_base.screenToNormalized(QPointF(event.position()))
        factor = 1.25
        if delta > 0:
            self._picture_base.zoomIn(factor, center)
        else:
            self._picture_base.zoomOut(factor, center)


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
        self.socket_client = None

    def set_service(self, service):
        self.socket_client = service

    def load_images(self, image_paths: list[str]):
        for split in self._splits:
            split.viewSynced.disconnect()
            split.cleanup()
            self._grid_layout.removeWidget(split)
            split.deleteLater()
        self._splits.clear()

        n = len(image_paths)
        if n == 0:
            return

        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        for i, path in enumerate(image_paths):
            split = _CompareSplit(self)
            split.viewSynced.connect(self._on_split_navigated)
            self._splits.append(split)
            self._grid_layout.addWidget(split, i // cols, i % cols)

        # Load images off the GUI thread
        for split, path in zip(self._splits, image_paths):
            threading.Thread(
                target=self._load_split_image, args=(split, path), daemon=True
            ).start()

    def _load_split_image(self, split: _CompareSplit, path: str):
        try:
            split.load_image(path, self.socket_client)
        except Exception as e:  # why: socket/plugin errors must not crash the worker thread
            logging.error(f"CompareView: failed to load {path}: {e}", exc_info=True)

    def _on_split_navigated(self, state: ViewState):
        if self._syncing:
            return
        self._syncing = True
        sender = self.sender()
        for split in self._splits:
            if split is not sender:
                split.apply_view_state(state.center, state.zoom, state.fit_mode)
        self._syncing = False

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
