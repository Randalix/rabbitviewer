# RabbitViewer Client-Daemon IPC Protocol

## 1. Overview

The GUI client and daemon communicate over a Unix domain socket at
`/tmp/rabbitviewer_{username}.sock`. There are two distinct channel types on
the same socket:

**Request/Response** — short-lived pooled connections. The client sends a
request and receives exactly one response.

**Notification** — one persistent connection per GUI. The client registers as a
listener and the daemon pushes events to it asynchronously.

---

## 2. Framing

### Client → Daemon (requests)

```
[4-byte big-endian length][JSON payload]
```

### Daemon → Client (responses)

```
[4-byte big-endian length][1-byte type discriminator][payload]
```

| Discriminator | Value | Payload |
|---|---|---|
| `FRAME_JSON`   | `0x00` | UTF-8 JSON |
| `FRAME_BINARY` | `0x01` | Raw image bytes |

Binary responses are only returned by `get_cached_view_image`.

### Daemon → Client (notifications)

```
[4-byte big-endian length][JSON payload]
```

No type discriminator — all notifications are JSON.

---

## 3. Common Structures

### ImageEntryModel

Used wherever a file path is sent or received. Bare strings are accepted on
input for backwards compatibility but `ImageEntryModel` objects are always
returned.

```json
{
  "path": "/abs/path/to/image.cr3",
  "sidecars": [],
  "variant": null
}
```

### PathPriority

Used in `update_viewport` to pair an entry with its heatmap-computed priority.

```json
{
  "entry": { "path": "/abs/path/to/image.cr3", "sidecars": [], "variant": null },
  "priority": 90
}
```

### Base Request fields

All requests include:

| Field | Type | Description |
|---|---|---|
| `command` | string | Command name |
| `session_id` | string\|null | GUI session UUID, auto-set by `ThumbnailSocketClient` |

### Base Response fields

All JSON responses include:

| Field | Type | Description |
|---|---|---|
| `status` | string | `"success"`, `"queued"`, or `"error"` |
| `message` | string\|null | Human-readable detail (always present on error) |

### Error Response

```json
{ "status": "error", "message": "Description of the error." }
```

---

## 4. Request/Response Commands

### `get_directory_files`

Returns all known image files for a directory from the daemon's database,
simultaneously triggering a reconciliation walk to discover new/deleted files.
Sets the active GUI session on the daemon.

Requires a non-empty `session_id`.

**Request:**
```json
{
  "command": "get_directory_files",
  "session_id": "uuid",
  "path": "/abs/path/to/directory",
  "recursive": true
}
```

**Response:**
```json
{
  "status": "success",
  "files": [
    { "path": "/abs/path/to/directory/image1.cr3", "sidecars": [], "variant": null }
  ],
  "thumbnail_paths": {
    "/abs/path/to/directory/image1.cr3": "/abs/path/to/cache/thumb_abc.jpg"
  }
}
```

`thumbnail_paths` maps source path → cached thumbnail path for entries that
already have a valid thumbnail on disk. Allows the GUI to skip a round-trip for
cached images.

---

### `request_previews`

Queues thumbnail and view-image generation for a list of files. Returns
immediately; results arrive via `previews_ready` notifications.

**Request:**
```json
{
  "command": "request_previews",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ],
  "priority": 50
}
```

**Response:**
```json
{ "status": "queued", "count": 1 }
```

---

### `get_previews_status`

Non-blocking check of thumbnail/view-image availability for a list of files.

**Request:**
```json
{
  "command": "get_previews_status",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ]
}
```

**Response:**
```json
{
  "status": "success",
  "statuses": {
    "/abs/path/image1.cr3": {
      "thumbnail_ready": true,
      "thumbnail_path": "/abs/path/cache/thumb_abc.jpg",
      "view_image_ready": true,
      "view_image_path": "/abs/path/cache/view_abc.jpg"
    }
  }
}
```

---

### `request_view_image`

Requests a full-resolution view image at `FULLRES_REQUEST` priority (highest).
Always returns JSON. If the image is in the daemon's in-memory cache, call
`get_cached_view_image` to retrieve the bytes. Otherwise waits for a
`previews_ready` notification.

**Request:**
```json
{
  "command": "request_view_image",
  "image_entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
}
```

**Response:**
```json
{
  "status": "success",
  "view_image_path": "/abs/path/cache/view_abc.jpg",
  "view_image_source": "disk"
}
```

`view_image_source` values:

| Value | Meaning |
|---|---|
| `"memory"` | Bytes are in daemon mem cache; call `get_cached_view_image` |
| `"disk"` | View image available at `view_image_path` |
| `"direct"` | Source file is natively viewable; path returned in `view_image_path` |
| `null` | Generation queued; wait for `previews_ready` notification |

---

### `get_cached_view_image`

Fetches bytes for a mem-cached view image. Returns a **binary response frame**
(`FRAME_BINARY`) if the image is in the daemon's in-memory LRU cache, or a JSON
miss response if the entry has been evicted.

**Request:**
```json
{
  "command": "get_cached_view_image",
  "image_entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
}
```

**Response (cache hit):** raw image bytes in a `FRAME_BINARY` response frame.

**Response (cache miss):**
```json
{ "status": "success", "message": "miss" }
```

---

### `set_rating`

Sets the star rating for a list of files. Updates the database immediately and
queues a background task to write the rating to each file's embedded metadata.

**Request:**
```json
{
  "command": "set_rating",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ],
  "rating": 3
}
```

`rating` must be 0–5.

**Response:**
```json
{ "status": "success", "message": "Ratings updated and queued for file write." }
```

---

### `get_metadata_batch`

Returns all known metadata for a list of files from the database.

**Request:**
```json
{
  "command": "get_metadata_batch",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ],
  "priority": false
}
```

If `priority` is `true`, high-priority metadata extraction is queued for any
files without cached metadata.

**Response:**
```json
{
  "status": "success",
  "metadata": {
    "/abs/path/image1.cr3": {
      "rating": 3,
      "width": 6000,
      "height": 4000,
      "camera_make": "Canon",
      "date_taken": 1700000000.0
    }
  }
}
```

---

### `update_viewport`

Sends heatmap-based priority updates to the daemon. Only delta changes are
sent; entries whose priority is unchanged since the last update are omitted.

**Request:**
```json
{
  "command": "update_viewport",
  "paths_to_upgrade": [
    { "entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }, "priority": 90 },
    { "entry": { "path": "/abs/path/image2.cr3", "sidecars": [], "variant": null }, "priority": 87 }
  ],
  "paths_to_downgrade": [
    { "path": "/abs/path/scrolled_away.cr3", "sidecars": [], "variant": null }
  ],
  "fullres_to_request": [
    { "entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }, "priority": 81 }
  ],
  "fullres_to_cancel": [
    { "path": "/abs/path/no_longer_nearby.cr3", "sidecars": [], "variant": null }
  ]
}
```

- `paths_to_upgrade` — thumbnails in the 10-ring heatmap diamond; ring 0 = priority 90, step −3 per ring.
- `paths_to_downgrade` — paths that scrolled out of the visible zone, demoted to `GUI_REQUEST_LOW` (40).
- `fullres_to_request` — speculative fullres pre-cache for the 4-ring zone (offset by 3 rings so thumbnails render first). Priorities interleave with thumbnail priorities.
- `fullres_to_cancel` — fullres tasks that left the 4-ring zone, cooperatively cancelled via `threading.Event`.

**Response:**
```json
{ "status": "success", "message": "2 upgraded" }
```

---

### `get_filtered_file_paths`

Returns file paths matching a text filter, star-rating filter, and/or tag
filter, as determined by the daemon's database.

**Request:**
```json
{
  "command": "get_filtered_file_paths",
  "text_filter": "sunset",
  "star_states": [false, false, false, true, true, true],
  "tag_names": ["travel", "landscape"]
}
```

`star_states` is a 6-element boolean array indexed 0–5; `true` means that star
count passes the filter.

**Response:**
```json
{
  "status": "success",
  "paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ]
}
```

---

### `set_tags`

Adds tags to a list of files. Updates the database and queues background tasks
to write tags to each file's embedded metadata.

**Request:**
```json
{
  "command": "set_tags",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ],
  "tags": ["travel", "landscape"]
}
```

**Response:**
```json
{ "status": "success", "message": "Tags updated and queued for file write." }
```

---

### `remove_tags`

Removes tags from a list of files. Updates the database and queues background
write-back.

**Request:**
```json
{
  "command": "remove_tags",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ],
  "tags": ["landscape"]
}
```

**Response:**
```json
{ "status": "success", "message": "Tags removed and queued for file write." }
```

---

### `get_tags`

Returns all known tags, split into directory-scoped and global lists.

**Request:**
```json
{
  "command": "get_tags",
  "directory_path": "/abs/path/to/directory"
}
```

**Response:**
```json
{
  "status": "success",
  "directory_tags": [{ "name": "travel", "kind": "keyword" }],
  "global_tags":    [{ "name": "landscape", "kind": "keyword" }]
}
```

---

### `get_image_tags`

Returns the tags currently assigned to each requested file.

**Request:**
```json
{
  "command": "get_image_tags",
  "image_paths": [
    { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
  ]
}
```

**Response:**
```json
{
  "status": "success",
  "tags": {
    "/abs/path/image1.cr3": ["travel", "landscape"]
  }
}
```

---

### `move_records`

Updates file-path records in the database after files have been moved on disk.

**Request:**
```json
{
  "command": "move_records",
  "moves": [
    {
      "old_entry": { "path": "/old/path/image1.cr3", "sidecars": [], "variant": null },
      "new_entry": { "path": "/new/path/image1.cr3", "sidecars": [], "variant": null }
    }
  ]
}
```

**Response:**
```json
{ "status": "success", "moved_count": 1 }
```

---

### `run_tasks`

Submits a compound task to the daemon for async execution. Each operation is a
named handler registered in `ThumbnailManager`. Operations execute sequentially
in a single `RenderManager` worker at `NORMAL` priority.

**Request:**
```json
{
  "command": "run_tasks",
  "operations": [
    { "name": "send2trash",     "file_paths": [{ "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }] },
    { "name": "remove_records", "file_paths": [{ "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }] }
  ]
}
```

**Response:**
```json
{ "status": "success", "task_id": "script_task::1", "queued_count": 2 }
```

---

### `comfyui_generate`

Queues a ComfyUI generation task for an image. The result arrives via a
`comfyui_complete` notification.

**Request:**
```json
{
  "command": "comfyui_generate",
  "image_entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null },
  "prompt": "make it look like a painting",
  "denoise": 0.30,
  "workflow": ""
}
```

`workflow` is either an empty string (use built-in Flux Kontext workflow) or a
full ComfyUI workflow JSON string.

**Response:**
```json
{ "status": "success", "task_id": "comfyui_task_abc123" }
```

---

### `shutdown`

Instructs the daemon to shut down.

**Request:**
```json
{ "command": "shutdown" }
```

**Response:**
```json
{ "status": "success", "message": "Server shutting down" }
```

---

## 5. Notification Channel

The GUI opens a dedicated long-lived connection and sends a single registration
message:

```json
{ "type": "register_notifier" }
```

The daemon keeps the connection open indefinitely and pushes notification
messages. The socket timeout is removed after registration so that blocking
`recv()` waits for data without timing out. The connection is closed by the GUI
on shutdown.

All notification messages share this envelope:

```json
{
  "type": "notification_type",
  "data": { ... },
  "session_id": "uuid-or-null"
}
```

`session_id` is set for notifications that belong to a specific GUI session
(e.g. scan progress). The client discards notifications whose `session_id`
does not match the active session.

---

## 6. Notifications

### `previews_ready`

Sent when a thumbnail and/or view image for a single file has been generated.

```json
{
  "type": "previews_ready",
  "data": {
    "image_entry": { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null },
    "thumbnail_path": "/abs/path/cache/thumb_abc.jpg",
    "view_image_path": "/abs/path/cache/view_abc.jpg",
    "view_image_source": "disk"
  },
  "session_id": "uuid"
}
```

`view_image_source` mirrors the values from `request_view_image` (`"disk"`,
`"memory"`, `"direct"`, or `null`). When `"memory"`, call
`get_cached_view_image` to retrieve the bytes.

---

### `scan_progress`

Sent periodically during a directory scan with newly discovered files.

```json
{
  "type": "scan_progress",
  "data": {
    "path": "/abs/path/to/directory",
    "files": [
      { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
    ]
  },
  "session_id": "uuid"
}
```

---

### `scan_complete`

Sent when a directory scan finishes.

```json
{
  "type": "scan_complete",
  "data": {
    "path": "/abs/path/to/directory",
    "file_count": 152,
    "files": [
      { "path": "/abs/path/image1.cr3", "sidecars": [], "variant": null }
    ]
  },
  "session_id": "uuid"
}
```

---

### `files_removed`

Sent when the reconciliation walk detects files that are in the database but
no longer present on disk (ghost records). The daemon removes the records from
its database after sending this notification.

```json
{
  "type": "files_removed",
  "data": {
    "files": [
      { "path": "/abs/path/deleted_image.cr3", "sidecars": [], "variant": null }
    ]
  },
  "session_id": "uuid"
}
```

---

### `comfyui_complete`

Sent when a ComfyUI generation task finishes (success or failure).

```json
{
  "type": "comfyui_complete",
  "data": {
    "source_path": "/abs/path/image1.cr3",
    "result_path": "/abs/path/image1_comfyui.png",
    "status": "success",
    "error": ""
  },
  "session_id": "uuid"
}
```

`status` is `"success"` or `"error"`. On error, `result_path` is empty and
`error` contains a description.
