# AI Image Editing via ComfyUI + FLUX Kontext

Research notes for integrating AI-powered image editing (color grading, style transfer, etc.) into RabbitViewer using a local ComfyUI instance running FLUX.1 Kontext.

## Overview

- RabbitViewer daemon connects to a ComfyUI instance on the local network over HTTP/WebSocket
- No GPU required on the viewer machine — ComfyUI runs on a dedicated GPU box
- No API costs — Kontext Dev is open-source and runs locally
- Original image resolution is preserved via a LUT extraction technique

## The Resolution Problem

FLUX.1 Kontext operates at ~1MP (~1024x1024 area, various aspect ratios). Source photos are typically 20-50MP. Sending the full image and getting back a 1MP result would be destructive.

### Solution: LUT Extraction

For **color grading** (a global color transform), we don't need pixel-level AI output at full resolution:

1. **Downscale** the original to fit Kontext's resolution (~1024px long edge)
2. **Send** to Kontext with a prompt like *"Apply warm cinematic color grading"*
3. **Extract a 3D color LUT** by comparing the input/output pixel colors
4. **Apply the LUT to the full-resolution original** using numpy/Pillow

This works because color grading maps input colors to output colors regardless of spatial position. The LUT captures that mapping and can be applied at any resolution.

### LUT Extraction Sketch

```python
import numpy as np
from PIL import Image

def extract_lut(original_small: Image, edited_small: Image, lut_size=64):
    """Compare input/output to build a 3D color LUT."""
    orig = np.array(original_small).reshape(-1, 3)
    edit = np.array(edited_small).reshape(-1, 3)

    lut = np.zeros((lut_size, lut_size, lut_size, 3), dtype=np.float64)
    counts = np.zeros((lut_size, lut_size, lut_size), dtype=np.float64)

    bins = (orig * (lut_size - 1) / 255).astype(int)
    for i in range(len(orig)):
        r, g, b = bins[i]
        lut[r, g, b] += edit[i]
        counts[r, g, b] += 1

    mask = counts > 0
    lut[mask] /= counts[mask, None]
    # Interpolate empty bins via scipy.ndimage or similar
    return lut

def apply_lut(full_res: Image, lut, lut_size=64):
    """Apply extracted LUT to full-resolution image via trilinear interpolation."""
    img = np.array(full_res, dtype=np.float64)
    # Trilinear interpolation into the LUT
    # (scipy.ndimage.map_coordinates or manual)
    return Image.fromarray(result.astype(np.uint8))
```

### When LUT Extraction Does NOT Work

| Approach | Good For | Bad For |
|---|---|---|
| **LUT extraction** | Color grading, tone mapping, film looks | Spatial edits (object removal, sky replacement) |
| **Tile-based processing** | Texture/style changes at full res | Seam artifacts, very slow, expensive |
| **Direct Kontext at model res** | Quick previews, social media output | Anything needing original resolution |

For spatial edits (object removal, sky replacement, etc.) at full resolution, a tile-based approach with overlap blending or a different model would be needed.

## ComfyUI API

ComfyUI exposes the following endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /upload/image` | Upload the input image |
| `POST /prompt` | Queue a workflow for execution |
| `GET /history/{prompt_id}` | Poll for results |
| `WS /ws?clientId=X` | Real-time progress updates |
| `GET /view?filename=X` | Download the output image |

Workflows are submitted as JSON graphs exported from ComfyUI via "Save (API Format)". Node inputs are patched at runtime.

## Integration Architecture

```
RabbitViewer GUI          RabbitViewer Daemon           ComfyUI (LAN)
     |                         |                     192.168.x.y:8188
     |                         |                            |
     +-- ai_edit_request ----->|                            |
     |  {path, prompt}         |                            |
     |                         +-- downscale to ~1024px     |
     |                         +-- POST /upload/image ----->|
     |                         +-- POST /prompt ----------->| (workflow JSON
     |                         |  (patched workflow)        |  with Kontext nodes)
     |                         |                            |
     |                         +-- WS /ws <----------------|  progress updates
     |<-- ai_edit_progress ----|  (% complete)              |
     |                         |                            |
     |                         +-- GET /view?filename= <---|  fetch result
     |                         +-- extract LUT              |
     |                         +-- apply LUT to full-res    |
     |                         +-- save result              |
     |<-- ai_edit_complete ----|                            |
```

### Key Daemon Components

**`core/comfyui_client.py`** — HTTP/WS client that talks to ComfyUI:

```python
import json
import uuid
import urllib.request
import urllib.parse
from pathlib import Path

class ComfyUIClient:
    """Talks to a ComfyUI instance on the local network."""

    def __init__(self, host: str = "192.168.1.100", port: int = 8188):
        self.base_url = f"http://{host}:{port}"
        self.client_id = str(uuid.uuid4())

    def upload_image(self, image_path: Path) -> str:
        """Upload image via multipart POST to /upload/image, return server filename."""
        ...

    def queue_workflow(self, workflow: dict) -> str:
        """Submit workflow JSON, return prompt_id."""
        payload = json.dumps({"prompt": workflow, "client_id": self.client_id})
        req = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        return resp["prompt_id"]

    def get_result(self, prompt_id: str) -> dict:
        """Poll /history/{prompt_id} for completion."""
        ...

    def download_image(self, filename: str, subfolder: str = "") -> bytes:
        """GET /view?filename=...&subfolder=...&type=output"""
        params = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": "output"}
        )
        return urllib.request.urlopen(f"{self.base_url}/view?{params}").read()
```

**Workflow template patching** — load exported JSON, swap in runtime values:

```python
def build_workflow(self, uploaded_filename: str, prompt: str) -> dict:
    workflow = json.loads(KONTEXT_TEMPLATE)

    # Patch the LoadImage node with our uploaded file
    workflow["10"]["inputs"]["image"] = uploaded_filename

    # Patch the prompt/CLIP text node
    workflow["6"]["inputs"]["text"] = prompt

    return workflow
```

Node IDs (`"10"`, `"6"`) come from the exported API JSON and are stable for a given workflow.

**`core/ai_editor.py`** — orchestrates the full pipeline (downscale, upload, queue, poll, download, LUT extract, apply, save). Runs as a `RenderTask` via `RenderManager` for priority/cancellation support.

### Workflow Templates

Stored as exported ComfyUI API-format JSON:

```
config/
  comfyui_workflows/
    kontext_color_grade.json
```

### IPC Commands

New protocol messages:

- `ai_edit_request` — GUI to daemon: `{command, path, prompt, workflow?}`
- `ai_edit_progress` — daemon notification: `{path, progress_pct}`
- `ai_edit_complete` — daemon notification: `{path, result_path}`

### Config

```yaml
comfyui:
  enabled: false
  host: "192.168.1.100"
  port: 8188
  workflow: "kontext_color_grade"   # name in config/comfyui_workflows/
```

## ComfyUI Kontext Setup (GPU Machine)

Requires ComfyUI v0.3.42+. Model files:

- `models/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors`
- `models/text_encoders/clip_l.safetensors`
- `models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors`
- `models/vae/ae.safetensors`

Start ComfyUI with `--listen 0.0.0.0` to accept LAN connections.

## Future Extensions

This design generalizes beyond color grading:

- **Style transfer** — different workflow template, same plumbing
- **AI upscaling** — skip the LUT step, take output directly (upscale models output higher res)
- **Background removal** — spatial edit, no LUT needed
- **Batch processing** — queue multiple images, ComfyUI handles its own queue
- **ControlNet / IP-Adapter** — any ComfyUI workflow can be templated and parameterized

## References

- [ComfyUI API Routes](https://docs.comfy.org/development/comfyui-server/comms_routes)
- [ComfyUI FLUX Kontext Dev Tutorial](https://docs.comfy.org/tutorials/flux/flux-1-kontext-dev)
- [ComfyUI FLUX Kontext Complete Guide](https://comfyui-wiki.com/en/tutorial/advanced/image/flux/flux-1-kontext)
- [Hosting a ComfyUI Workflow via API](https://9elements.com/blog/hosting-a-comfyui-workflow-via-api/)
- [FLUX.1 Kontext — Black Forest Labs](https://bfl.ai/models/flux-kontext)
- [FLUX.1 Kontext Max — fal.ai](https://fal.ai/models/fal-ai/flux-pro/kontext/max/api)
