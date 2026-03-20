from PySide6.QtCore import QObject, QPointF
from typing import Dict, List, Callable, Optional, Set, FrozenSet, Tuple
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
import logging
logger = logging.getLogger(__name__)
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
    NAVIGATE_PARENT = "navigate_parent"
    
    # View events
    VIEW_CHANGE = "view_change"
    THUMBNAIL_DOUBLE_CLICK = "thumbnail_double_click"
    ESCAPE_PRESSED = "escape_pressed"
    
    # Inspector events
    INSPECTOR_UPDATE = "inspector_update"
    
    # UI command events
    OPEN_BREADCRUMB = "open_breadcrumb"
    OPEN_FILTER = "open_filter"
    OPEN_RATING_FILTER = "open_rating_filter"
    OPEN_DATE_FILTER = "open_date_filter"
    OPEN_NAME_FILTER = "open_name_filter"
    TOGGLE_DUPLICATES_FILTER = "toggle_duplicates_filter"
    OPEN_TAG_EDITOR = "open_tag_editor"
    OPEN_TAG_FILTER = "open_tag_filter"
    OPEN_COMPARE_GRID = "open_compare_grid"
    OPEN_COMPARE_SPLIT = "open_compare_split"
    SHIFT_PRESSED = "shift_pressed"
    SHIFT_RELEASED = "shift_released"
    TOGGLE_INSPECTOR = "toggle_inspector"

    # Overlay events
    THUMBNAIL_OVERLAY = "thumbnail_overlay"

    # AI / CLIP search
    OPEN_CLIP_SEARCH = "open_clip_search"

    # Similarity filter
    OPEN_SIMILARITY_FILTER = "open_similarity_filter"

    # Face recognition
    FACE_PERSON_FILTER = "face_person_filter"
    TOGGLE_FACE_PALETTE = "toggle_face_palette"

    # Thumbnail hover events
    THUMBNAIL_HOVERED = "thumbnail_hovered"
    THUMBNAIL_LEFT = "thumbnail_left"

    # Filter events
    TEXT_FILTER_CHANGED = "text_filter_changed"
    STAR_FILTER_CHANGED = "star_filter_changed"
    TAG_FILTER_CHANGED = "tag_filter_changed"
    DUPLICATES_FILTER_CHANGED = "duplicates_filter_changed"
    SELECTION_FILTER_CHANGED = "selection_filter_changed"
    DATE_FILTER_CHANGED = "date_filter_changed"
    CLEAR_FILTERS = "clear_filters"

    # pHash background progress
    PHASH_PROGRESS = "phash_progress"

    # Script / menu events
    RUN_SCRIPT = "run_script"
    OPEN_MODAL_MENU = "open_modal_menu"

    # Directory events
    NAVIGATE_TO_FOLDER = "navigate_to_folder"
    OPEN_RECENT_DIRECTORY = "open_recent_directory"

    # Status messages
    STATUS_MESSAGE = "status_message"


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
    cache_only: bool = False  # when True, show cached thumbnail/fullres only (no new requests)


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
class ThumbnailHoveredEventData(EventData):
    path: str

@dataclass
class TextFilterEventData(EventData):
    filter_text: str

@dataclass
class StarFilterEventData(EventData):
    star_states: list  # 6-element bool list

@dataclass
class TagFilterEventData(EventData):
    tag_names: list  # list of tag name strings

@dataclass
class DuplicatesFilterEventData(EventData):
    duplicates_only: bool

@dataclass
class PHashProgressEventData(EventData):
    file_path: str

@dataclass
class RunScriptEventData(EventData):
    script_name: str

@dataclass
class OpenModalMenuEventData(EventData):
    menu_id: str

@dataclass
class FacePersonFilterEventData(EventData):
    person_ids: List[str]  # empty = clear filter

@dataclass
class DateFilterEventData(EventData):
    date_range: Optional[Tuple[float, float]]  # (min_ts, max_ts) epoch seconds, or None to clear


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
class DirectoryEventData(EventData):
    path: str = ""

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
        logger.debug(f"Subscribed to {event_type.value}: {callback.__name__}")

    def unsubscribe(self, event_type: EventType, callback: Callable[[EventData], None]):
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logger.debug(f"Unsubscribed from {event_type.value}: {callback.__name__}")
                except ValueError:
                    logger.warning(f"Callback not found for {event_type.value}")

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
                logger.error(f"Error in event callback for {event_type.value}: {e}", exc_info=True)

        logger.debug("Published event: %s from %s", event_type.value, event_data.source)
        
    def get_event_history(self, event_type: Optional[EventType] = None) -> List[EventData]:
        if event_type:
            return [e for e in self._event_history if e.event_type == event_type]
        return list(self._event_history)
        
    def clear_history(self):
        self._event_history.clear()


event_system = EventSystem()
