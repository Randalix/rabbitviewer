# tests/test_inspector_view_modes.py
"""
Unit tests for InspectorView mode transitions.

Validates that zoom (wheel, right-drag) stays in tracking mode, double-click
cycles between tracking and fit, and only left-drag panning enters manual mode.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Qt stubs — InspectorView needs more surface than the global conftest provides.
# ---------------------------------------------------------------------------

def _ensure_qt_stubs():
    """Extend the PySide6 stubs with QWidget, QImage, QSettings, etc."""
    qtcore = sys.modules["PySide6.QtCore"]

    # Ensure QPoint exists
    if not hasattr(qtcore, "QPoint"):
        class _QPoint:
            def __init__(self, x=0, y=0):
                self._x = x; self._y = y
            def x(self): return self._x
            def y(self): return self._y
            def __sub__(self, other):
                return _QPoint(self._x - other._x, self._y - other._y)
        qtcore.QPoint = _QPoint

    if not hasattr(qtcore, "Slot"):
        qtcore.Slot = lambda *a, **kw: (lambda fn: fn)

    if not hasattr(qtcore, "QSettings"):
        class _QSettings:
            def __init__(self, *a): pass
            def value(self, key, default=None): return default
            def setValue(self, key, val): pass
            def sync(self): pass
        qtcore.QSettings = _QSettings

    # QSizeF / QRectF for PictureBase
    if not hasattr(qtcore, "QSizeF"):
        class _QSizeF:
            def __init__(self, w=0, h=0): self._w = w; self._h = h
            def width(self): return self._w
            def height(self): return self._h
        qtcore.QSizeF = _QSizeF

    if not hasattr(qtcore, "QRectF"):
        class _QRectF:
            def __init__(self, *a): pass
            def width(self): return 0
            def height(self): return 0
            def top(self): return 0
        qtcore.QRectF = _QRectF

    # QtGui
    qtgui = sys.modules.get("PySide6.QtGui")
    if qtgui is None:
        qtgui = types.ModuleType("PySide6.QtGui")
        sys.modules["PySide6.QtGui"] = qtgui
        sys.modules["PySide6"].QtGui = qtgui

    if not hasattr(qtgui, "QImage"):
        class _QImage:
            Format_RGBA8888 = 0
            def __init__(self, *a): pass
            def isNull(self): return True
            def copy(self): return self
        qtgui.QImage = _QImage

    if not hasattr(qtgui, "QPainter"):
        class _QPainter:
            SmoothPixmapTransform = 0
            def __init__(self, *a): pass
        qtgui.QPainter = _QPainter

    if not hasattr(qtgui, "QTransform"):
        class _QTransform:
            def __init__(self): pass
            def inverted(self): return self, True
            def map(self, pt): return pt
        qtgui.QTransform = _QTransform

    # QtWidgets
    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    if qtwidgets is None:
        qtwidgets = types.ModuleType("PySide6.QtWidgets")
        sys.modules["PySide6.QtWidgets"] = qtwidgets
        sys.modules["PySide6"].QtWidgets = qtwidgets

    if not hasattr(qtwidgets, "QWidget"):
        class _QWidget:
            def __init__(self, *a, **kw): pass
            def setWindowTitle(self, t): pass
            def setMinimumSize(self, w, h): pass
            def resize(self, w, h): pass
            def restoreGeometry(self, g): pass
            def saveGeometry(self): return b""
            def size(self): return qtcore.QSizeF(300, 300)
            def width(self): return 300
            def height(self): return 300
            def rect(self): return qtcore.QRectF()
            def isVisible(self): return True
            def setCursor(self, c): pass
            def update(self): pass
            def showEvent(self, e): pass
            def resizeEvent(self, e): pass
            def mousePressEvent(self, e): pass
            def mouseReleaseEvent(self, e): pass
            def mouseMoveEvent(self, e): pass
            def mouseDoubleClickEvent(self, e): pass
        qtwidgets.QWidget = _QWidget

    # Qt namespace constants
    qt = qtcore.Qt
    if not hasattr(qt, "Window"):
        qt.Window = 0
        qt.LeftButton = 1
        qt.RightButton = 2
        qt.ArrowCursor = 0
        qt.black = 0

    if not hasattr(qt, "CursorShape"):
        class _CursorShape:
            ClosedHandCursor = 1
        qt.CursorShape = _CursorShape


_ensure_qt_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeMouseEvent:
    """Minimal mouse event for testing handler dispatch."""
    def __init__(self, button=None, x=150.0, y=150.0):
        self._button = button
        self._x = x
        self._y = y

    def button(self):
        return self._button

    def position(self):
        return self

    def toPoint(self):
        from PySide6.QtCore import QPoint
        return QPoint(int(self._x), int(self._y))

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeWheelEvent:
    """Minimal wheel event."""
    def __init__(self, delta_y=120):
        self._dy = delta_y
        self.accepted = False

    def angleDelta(self):
        return self

    def y(self):
        return self._dy

    def accept(self):
        self.accepted = True


@pytest.fixture()
def inspector():
    """Create an InspectorView with mocked dependencies."""
    from gui.inspector_view import InspectorView, _ViewMode

    with patch("gui.inspector_view.event_system"):
        iv = InspectorView(config_manager=None, inspector_index=0)

    # Stub PictureBase methods used during mode transitions.
    iv._picture_base = MagicMock()
    iv._picture_base.has_image.return_value = True
    iv._picture_base.isDragZooming.return_value = False
    # calculateTransform().inverted() must return (transform, bool)
    mock_transform = MagicMock()
    mock_transform.inverted.return_value = (MagicMock(), True)
    iv._picture_base.calculateTransform.return_value = mock_transform
    iv._current_image_path = "/fake/image.jpg"
    iv._view_image_ready = True

    return iv, _ViewMode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWheelZoomDoesNotLock:
    """Wheel zoom must stay in tracking mode, never enter manual/locked."""

    def test_wheel_from_tracking_stays_tracking(self, inspector):
        iv, Mode = inspector
        iv._view_mode = Mode.TRACKING

        iv.wheelEvent(FakeWheelEvent(delta_y=120))

        assert iv._view_mode == Mode.TRACKING

    def test_wheel_from_fit_goes_to_tracking(self, inspector):
        iv, Mode = inspector
        iv._view_mode = Mode.FIT

        iv.wheelEvent(FakeWheelEvent(delta_y=120))

        assert iv._view_mode == Mode.TRACKING

    def test_wheel_from_manual_stays_manual(self, inspector):
        """If the user already locked, wheel zoom doesn't change the mode."""
        iv, Mode = inspector
        iv._view_mode = Mode.MANUAL

        iv.wheelEvent(FakeWheelEvent(delta_y=-120))

        assert iv._view_mode == Mode.MANUAL


class TestDoubleClickCycles:
    """Double-click must toggle between tracking and fit."""

    def test_tracking_to_fit(self, inspector):
        iv, Mode = inspector
        iv._view_mode = Mode.TRACKING
        Qt = sys.modules["PySide6.QtCore"].Qt

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))

        assert iv._view_mode == Mode.FIT
        iv._picture_base.setFitMode.assert_called_with(True)

    def test_fit_to_tracking(self, inspector):
        iv, Mode = inspector
        iv._view_mode = Mode.FIT
        Qt = sys.modules["PySide6.QtCore"].Qt

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))

        assert iv._view_mode == Mode.TRACKING
        iv._picture_base.setFitMode.assert_called_with(False)
        iv._picture_base.setZoom.assert_called()

    def test_manual_to_fit(self, inspector):
        """From locked/manual, double-click goes to fit."""
        iv, Mode = inspector
        iv._view_mode = Mode.MANUAL
        Qt = sys.modules["PySide6.QtCore"].Qt

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))

        assert iv._view_mode == Mode.FIT

    def test_full_cycle(self, inspector):
        """tracking → fit → tracking → fit round-trip."""
        iv, Mode = inspector
        Qt = sys.modules["PySide6.QtCore"].Qt
        iv._view_mode = Mode.TRACKING

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))
        assert iv._view_mode == Mode.FIT

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))
        assert iv._view_mode == Mode.TRACKING

        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))
        assert iv._view_mode == Mode.FIT

    def test_double_click_not_blocked_by_press(self, inspector):
        """The real Qt sequence: press → release → double-click → press.

        mousePressEvent must NOT enter manual mode, so the double-click
        handler sees the original mode and toggles correctly.
        """
        iv, Mode = inspector
        Qt = sys.modules["PySide6.QtCore"].Qt
        iv._view_mode = Mode.TRACKING

        # Simulate Qt's double-click delivery sequence
        iv.mousePressEvent(FakeMouseEvent(button=Qt.LeftButton))        # 1st click
        assert iv._view_mode == Mode.TRACKING, "press must not switch to manual"

        iv.mouseReleaseEvent(FakeMouseEvent(button=Qt.LeftButton))
        iv.mouseDoubleClickEvent(FakeMouseEvent(button=Qt.LeftButton))  # 2nd click
        assert iv._view_mode == Mode.FIT

        iv.mousePressEvent(FakeMouseEvent(button=Qt.LeftButton))        # Qt re-fires press
        assert iv._view_mode == Mode.FIT, "press after dbl-click must not change mode"


class TestPanningEntersManual:
    """Only left-button drag (actual movement) should enter manual mode."""

    def test_press_alone_does_not_lock(self, inspector):
        iv, Mode = inspector
        Qt = sys.modules["PySide6.QtCore"].Qt
        iv._view_mode = Mode.TRACKING

        iv.mousePressEvent(FakeMouseEvent(button=Qt.LeftButton))
        iv.mouseReleaseEvent(FakeMouseEvent(button=Qt.LeftButton))

        assert iv._view_mode == Mode.TRACKING

    def test_drag_enters_manual(self, inspector):
        iv, Mode = inspector
        Qt = sys.modules["PySide6.QtCore"].Qt
        iv._view_mode = Mode.TRACKING

        iv.mousePressEvent(FakeMouseEvent(button=Qt.LeftButton, x=100, y=100))
        iv.mouseMoveEvent(FakeMouseEvent(button=Qt.LeftButton, x=120, y=120))

        assert iv._view_mode == Mode.MANUAL


class TestRightDragZoomDoesNotLock:
    """Right-click drag zoom must not enter manual/locked mode."""

    def test_right_drag_stays_tracking(self, inspector):
        iv, Mode = inspector
        Qt = sys.modules["PySide6.QtCore"].Qt
        iv._view_mode = Mode.TRACKING
        iv._picture_base.isDragZooming.return_value = True
        iv._picture_base.computeDragZoom.return_value = 4.0

        iv.mousePressEvent(FakeMouseEvent(button=Qt.RightButton, x=100, y=100))
        iv.mouseMoveEvent(FakeMouseEvent(button=Qt.RightButton, x=130, y=100))

        assert iv._view_mode == Mode.TRACKING
