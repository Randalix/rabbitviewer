from pydantic import BaseModel, Field
from typing import Any, List, Dict, Optional

# ==============================================================================
#  Base Models & Common Structures
# ==============================================================================

class Request(BaseModel):
    """Base model for all client-to-server requests."""
    command: str
    session_id: Optional[str] = None

class Response(BaseModel):
    """Base model for all server-to-client responses."""
    status: str = "success"
    message: Optional[str] = None

class ErrorResponse(Response):
    """Standardized error response."""
    status: str = "error"
    message: str

class PreviewStatus(BaseModel):
    """Represents the readiness of previews for a single image."""
    thumbnail_ready: bool
    thumbnail_path: Optional[str] = None
    view_image_ready: bool
    view_image_path: Optional[str] = None

# ==============================================================================
#  Request/Response Models
# ==============================================================================

# --- Get Directory Files ---
class GetDirectoryFilesRequest(Request):
    command: str = "get_directory_files"
    path: str
    recursive: bool = True

class GetDirectoryFilesResponse(Response):
    files: List[str]

# --- Request Previews ---
class RequestPreviewsRequest(Request):
    command: str = "request_previews"
    image_paths: List[str]
    priority: int

class RequestPreviewsResponse(Response):
    status: str = "queued"
    count: int

# --- Get Previews Status ---
class GetPreviewsStatusRequest(Request):
    command: str = "get_previews_status"
    image_paths: List[str]

class GetPreviewsStatusResponse(Response):
    statuses: Dict[str, PreviewStatus]

# --- Set Rating ---
class SetRatingRequest(Request):
    command: str = "set_rating"
    image_paths: List[str]
    rating: int = Field(..., ge=0, le=5)

# --- Get Metadata ---
class GetMetadataBatchRequest(Request):
    command: str = "get_metadata_batch"
    image_paths: List[str]
    priority: bool = False

class GetMetadataBatchResponse(Response):
    metadata: Dict[str, Dict[str, Any]]

# --- Update Viewport (upgrade visible + downgrade scrolled-away thumbnails) ---
class UpdateViewportRequest(Request):
    command: str = "update_viewport"
    paths_to_upgrade: List[str]
    paths_to_downgrade: List[str]

# --- Request View Image (FULLRES_REQUEST priority) ---
class RequestViewImageRequest(Request):
    command: str = "request_view_image"
    image_path: str

class RequestViewImageResponse(Response):
    view_image_path: Optional[str] = None  # Non-None when view image was already cached

# --- Get Filtered File Paths ---
class GetFilteredFilePathsRequest(Request):
    command: str = "get_filtered_file_paths"
    text_filter: str
    star_states: List[bool]

class GetFilteredFilePathsResponse(Response):
    paths: List[str]

class Notification(BaseModel):
    """Base model for all server-to-client notifications."""
    type: str
    data: Dict[str, Any]  # why: typed at construction (XxxData.model_dump()); validated at consumption (XxxData.model_validate()); loose dict is an intentional serialization seam
    session_id: Optional[str] = None

# ==============================================================================
#  Move Records (Daemon)
# ==============================================================================

class MoveRecord(BaseModel):
    old_path: str
    new_path: str

class MoveRecordsRequest(Request):
    command: str = "move_records"
    moves: List[MoveRecord]

class MoveRecordsResponse(Response):
    moved_count: int

# ==============================================================================
#  GUI Server Protocol
# ==============================================================================

class GuiRequest(BaseModel):
    """Base model for GUI server requests."""
    command: str

class GetSelectionResponse(BaseModel):
    status: str = "success"
    paths: List[str]

class RemoveImagesRequest(GuiRequest):
    command: str = "remove_images"
    paths: List[str]

class ClearSelectionRequest(GuiRequest):
    command: str = "clear_selection"

class GuiSuccessResponse(BaseModel):
    status: str = "success"

class GuiErrorResponse(BaseModel):
    status: str = "error"
    message: str

# ==============================================================================
#  Notification Models
# ==============================================================================

class ScanCompleteData(BaseModel):
    path: str
    file_count: int
    files: List[str]

class ScanProgressData(BaseModel):
    path: str
    files: List[str]

class PreviewsReadyData(BaseModel):
    image_path: str
    thumbnail_path: Optional[str]
    view_image_path: Optional[str]

class FilesRemovedData(BaseModel):
    files: List[str]
