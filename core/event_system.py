from PySide6.QtCore import QObject, QPointF
from typing import Dict, List, Callable, Optional, Set, FrozenSet
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
import logging
import threading
import time


class EventType(Enum):
    # Mouse events
    MOUSE_PRESS = "mouse_press"
    MOUSE_RELEASE = "mouse_release"
    MOUSE_MOVE = "mouse_move"
    MOUSE_DOUBLE_CLICK = "mouse_double_click"
    MOUSE_ENTER = "mouse_enter"
    MOUSE_LEAVE = "mouse_leave"
    
    # Keyboard events
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    
    # Selection events
    EXECUTE_SELECTION_COMMAND = "execute_selection_command" # A component wants to change the selection
    SELECTION_CHANGED = "selection_changed" # The selection state has officially changed
    UNDO_SELECTION = "undo_selection" # To trigger an undo from a hotkey
    REDO_SELECTION = "redo_selection" # To trigger a redo from a hotkey
    
    # Navigation events
    NAVIGATE_NEXT = "navigate_next"
    NAVIGATE_PREVIOUS = "navigate_previous"
    
    # View events
    VIEW_CHANGE = "view_change"
    THUMBNAIL_DOUBLE_CLICK = "thumbnail_double_click"
    ESCAPE_PRESSED = "escape_pressed"
    
    # Inspector events
    INSPECTOR_UPDATE = "inspector_update"
    
    # Zoom events
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    ZOOM_TO_POINT = "zoom_to_point"
    ZOOM_FIT = "zoom_fit"
    ZOOM_RESET = "zoom_reset"
    ZOOM_DRAG_START = "zoom_drag_start"
    ZOOM_DRAG_UPDATE = "zoom_drag_update"
    ZOOM_DRAG_END = "zoom_drag_end"
    DOUBLE_CLICK_ZOOM = "double_click_zoom"
    
    # UI command events
    OPEN_FILTER = "open_filter"
    OPEN_TAG_EDITOR = "open_tag_editor"
    OPEN_TAG_FILTER = "open_tag_filter"
    RANGE_SELECTION_START = "range_selection_start"
    RANGE_SELECTION_END = "range_selection_end"
    TOGGLE_INSPECTOR = "toggle_inspector"

    # Overlay events
    THUMBNAIL_OVERLAY = "thumbnail_overlay"

    # Status messages
    STATUS_MESSAGE = "status_message"
    DAEMON_NOTIFICATION = "daemon_notification"


@dataclass
class EventData:
    event_type: EventType
    source: str  # Source widget/component name
    timestamp: float
    
    
@dataclass
class MouseEventData(EventData):
    button: int
    position: QPointF
    global_position: QPointF
    modifiers: int
    thumbnail_index: Optional[int] = None
    

@dataclass
class KeyEventData(EventData):
    key: int
    modifiers: int
    text: str
    auto_repeat: bool = False
    

@dataclass
class NavigationEventData(EventData):
    direction: str
    current_path: Optional[str] = None
    target_path: Optional[str] = None
    

@dataclass
class ViewEventData(EventData):
    view_name: str
    image_path: Optional[str] = None
    

@dataclass
class InspectorEventData(EventData):
    image_path: str
    normalized_position: QPointF


@dataclass
class ZoomEventData(EventData):
    zoom_factor: float
    center_point: Optional[QPointF] = None  # Normalized coordinates (0-1)
    fit_mode: bool = False


@dataclass
class ZoomDragEventData(EventData):
    anchor_point: QPointF  # Normalized coordinates (0-1)
    current_position: QPointF  # Screen coordinates
    start_position: QPointF  # Screen coordinates
    initial_zoom: float


@dataclass
class DoubleClickZoomEventData(EventData):
    click_position: QPointF  # Normalized coordinates (0-1)
    current_zoom: float
    is_fit_mode: bool


class StatusSection(Enum):
    FILEPATH = "filepath"
    RATING   = "rating"
    PROCESS  = "process"


@dataclass
class StatusMessageEventData(EventData):
    message: str
    timeout: int = 0
    permanent: bool = False
    section: StatusSection = StatusSection.PROCESS


@dataclass
class SelectionChangedEventData(EventData):
    selected_paths: FrozenSet[str]

@dataclass
class ThumbnailOverlayEventData(EventData):
    action: str  # "show" or "remove"
    paths: list
    overlay_id: str
    renderer_name: str = ""
    params: dict = field(default_factory=dict)
    position: str = "center"
    duration: Optional[int] = None  # ms; None = permanent

@dataclass
class DaemonNotificationEventData(EventData):
    notification_type: str
    data: dict  # intentional: loose contract — daemon payloads vary by notification_type


# High-frequency events that are not appended to history to avoid evicting
# genuinely useful events and to reduce lock hold time.
_EPHEMERAL_EVENT_TYPES: frozenset = frozenset({EventType.INSPECTOR_UPDATE, EventType.THUMBNAIL_OVERLAY})


class EventSystem(QObject):
    def __init__(self):
        super().__init__()
        self._subscribers: Dict[EventType, List[Callable]] = {}
        self._event_history: deque[EventData] = deque(maxlen=500)
        self._lock = threading.Lock()

    def subscribe(self, event_type: EventType, callback: Callable[[EventData], None]):
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
        logging.debug(f"Subscribed to {event_type.value}: {callback.__name__}")

    def unsubscribe(self, event_type: EventType, callback: Callable[[EventData], None]):
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logging.debug(f"Unsubscribed from {event_type.value}: {callback.__name__}")
                except ValueError:
                    logging.warning(f"Callback not found for {event_type.value}")

    def publish(self, event_data: EventData):
        with self._lock:
            event_type = event_data.event_type
            if event_type not in _EPHEMERAL_EVENT_TYPES:
                self._event_history.append(event_data)
            # Snapshot the subscriber list so callbacks can safely call subscribe/unsubscribe.
            callbacks = list(self._subscribers.get(event_type, []))

        for callback in callbacks:
            try:
                callback(event_data)
            except Exception as e:
                # why: isolate handler crashes so one broken subscriber can't block others
                logging.error(f"Error in event callback for {event_type.value}: {e}", exc_info=True)

        logging.debug("Published event: %s from %s", event_type.value, event_data.source)
        
    def get_event_history(self, event_type: Optional[EventType] = None) -> List[EventData]:
        if event_type:
            return [e for e in self._event_history if e.event_type == event_type]
        return list(self._event_history)
        
    def clear_history(self):
        self._event_history.clear()


event_system = EventSystem()
