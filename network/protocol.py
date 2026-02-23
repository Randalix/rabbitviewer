import dataclasses
import json
import typing
from typing import Any, List, Dict, Optional


# ==============================================================================
#  Base Message class (replaces pydantic.BaseModel)
# ==============================================================================

@dataclasses.dataclass
class Message:
    """Base for all protocol models. Provides dict/JSON round-trip."""

    @classmethod
    def model_validate(cls, data: dict):
        """Construct from dict, recursively hydrating nested Message fields."""
        hints = typing.get_type_hints(cls)
        kwargs = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            val = data[f.name]
            hint = hints.get(f.name)
            origin = getattr(hint, '__origin__', None)
            # List[MessageSubclass]
            if origin is list and val:
                inner = getattr(hint, '__args__', (None,))[0]
                if inner and isinstance(inner, type) and issubclass(inner, Message):
                    val = [inner.model_validate(v) if isinstance(v, dict) else v for v in val]
            # Dict[str, MessageSubclass]
            elif origin is dict and val:
                args = getattr(hint, '__args__', ())
                inner = args[1] if len(args) > 1 else None
                if inner and isinstance(inner, type) and issubclass(inner, Message):
                    val = {k: inner.model_validate(v) if isinstance(v, dict) else v for k, v in val.items()}
            # Bare MessageSubclass field
            elif isinstance(hint, type) and issubclass(hint, Message) and isinstance(val, dict):
                val = hint.model_validate(val)
            kwargs[f.name] = val
        return cls(**kwargs)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump())


# ==============================================================================
#  Base Models & Common Structures
# ==============================================================================

@dataclasses.dataclass
class Request(Message):
    """Base model for all client-to-server requests."""
    command: str = ""
    session_id: Optional[str] = None

@dataclasses.dataclass
class Response(Message):
    """Base model for all server-to-client responses."""
    status: str = "success"
    message: Optional[str] = None

@dataclasses.dataclass
class ErrorResponse(Response):
    """Standardized error response."""
    status: str = "error"
    message: str = ""

@dataclasses.dataclass
class PreviewStatus(Message):
    """Represents the readiness of previews for a single image."""
    thumbnail_ready: bool = False
    thumbnail_path: Optional[str] = None
    view_image_ready: bool = False
    view_image_path: Optional[str] = None

# ==============================================================================
#  Request/Response Models
# ==============================================================================

# --- Get Directory Files ---
@dataclasses.dataclass
class GetDirectoryFilesRequest(Request):
    command: str = "get_directory_files"
    path: str = ""
    recursive: bool = True

@dataclasses.dataclass
class GetDirectoryFilesResponse(Response):
    files: List[str] = dataclasses.field(default_factory=list)

# --- Request Previews ---
@dataclasses.dataclass
class RequestPreviewsRequest(Request):
    command: str = "request_previews"
    image_paths: List[str] = dataclasses.field(default_factory=list)
    priority: int = 0

@dataclasses.dataclass
class RequestPreviewsResponse(Response):
    status: str = "queued"
    count: int = 0

# --- Get Previews Status ---
@dataclasses.dataclass
class GetPreviewsStatusRequest(Request):
    command: str = "get_previews_status"
    image_paths: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class GetPreviewsStatusResponse(Response):
    statuses: Dict[str, PreviewStatus] = dataclasses.field(default_factory=dict)

# --- Set Rating ---
@dataclasses.dataclass
class SetRatingRequest(Request):
    command: str = "set_rating"
    image_paths: List[str] = dataclasses.field(default_factory=list)
    rating: int = 0

    def __post_init__(self):
        if not (0 <= self.rating <= 5):
            raise ValueError(f"rating must be 0-5, got {self.rating}")

# --- Get Metadata ---
@dataclasses.dataclass
class GetMetadataBatchRequest(Request):
    command: str = "get_metadata_batch"
    image_paths: List[str] = dataclasses.field(default_factory=list)
    priority: bool = False

@dataclasses.dataclass
class GetMetadataBatchResponse(Response):
    metadata: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)

# --- Update Viewport (upgrade visible + downgrade scrolled-away thumbnails) ---
@dataclasses.dataclass
class UpdateViewportRequest(Request):
    command: str = "update_viewport"
    paths_to_upgrade: List[str] = dataclasses.field(default_factory=list)
    paths_to_downgrade: List[str] = dataclasses.field(default_factory=list)

# --- Request View Image (FULLRES_REQUEST priority) ---
@dataclasses.dataclass
class RequestViewImageRequest(Request):
    command: str = "request_view_image"
    image_path: str = ""

@dataclasses.dataclass
class RequestViewImageResponse(Response):
    view_image_path: Optional[str] = None

# --- Get Filtered File Paths ---
@dataclasses.dataclass
class GetFilteredFilePathsRequest(Request):
    command: str = "get_filtered_file_paths"
    text_filter: str = ""
    star_states: List[bool] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class GetFilteredFilePathsResponse(Response):
    paths: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class Notification(Message):
    """Base model for all server-to-client notifications."""
    type: str = ""
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)  # why: typed at construction (XxxData.model_dump()); validated at consumption (XxxData.model_validate()); loose dict is an intentional serialization seam
    session_id: Optional[str] = None

# ==============================================================================
#  Move Records (Daemon)
# ==============================================================================

@dataclasses.dataclass
class MoveRecord(Message):
    old_path: str = ""
    new_path: str = ""

@dataclasses.dataclass
class MoveRecordsRequest(Request):
    command: str = "move_records"
    moves: List[MoveRecord] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class MoveRecordsResponse(Response):
    moved_count: int = 0

# ==============================================================================
#  Run Tasks (Generic Daemon Task Dispatch)
# ==============================================================================

@dataclasses.dataclass
class TaskOperation(Message):
    """A single named operation with file paths to operate on."""
    name: str = ""
    file_paths: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class RunTasksRequest(Request):
    command: str = "run_tasks"
    operations: List[TaskOperation] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class RunTasksResponse(Response):
    task_id: str = ""
    queued_count: int = 0

# ==============================================================================
#  GUI Server Protocol
# ==============================================================================

@dataclasses.dataclass
class GuiRequest(Message):
    """Base model for GUI server requests."""
    command: str = ""

@dataclasses.dataclass
class GetSelectionResponse(Message):
    status: str = "success"
    paths: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class RemoveImagesRequest(GuiRequest):
    command: str = "remove_images"
    paths: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class ClearSelectionRequest(GuiRequest):
    command: str = "clear_selection"

@dataclasses.dataclass
class GuiSuccessResponse(Message):
    status: str = "success"

@dataclasses.dataclass
class GuiErrorResponse(Message):
    status: str = "error"
    message: str = ""

# ==============================================================================
#  Notification Models
# ==============================================================================

@dataclasses.dataclass
class ScanCompleteData(Message):
    path: str = ""
    file_count: int = 0
    files: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class ScanProgressData(Message):
    path: str = ""
    files: List[str] = dataclasses.field(default_factory=list)

@dataclasses.dataclass
class PreviewsReadyData(Message):
    image_path: str = ""
    thumbnail_path: Optional[str] = None
    view_image_path: Optional[str] = None

@dataclasses.dataclass
class FilesRemovedData(Message):
    files: List[str] = dataclasses.field(default_factory=list)
