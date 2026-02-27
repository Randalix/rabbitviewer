"""ComfyUI HTTP client for the daemon side.

Pure stdlib — no Qt dependency.  Runs on RenderManager worker threads.
"""

import json
import logging
import mimetypes
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ComfyUIClient:
    """Talks to a ComfyUI server over REST to run Flux Kontext workflows."""

    def __init__(self, host: str = "192.168.50.4", port: int = 8188):
        self.base_url = f"http://{host}:{port}"
        self.client_id = str(uuid.uuid4())

    # ── Public API ───────────────────────────────────────────────

    def generate(
        self,
        image_path: str,
        prompt: str,
        denoise: float,
        cancel_event=None,
        timeout: int = 300,
        workflow_json: str = "",
    ) -> Optional[str]:
        """Upload image, run workflow, poll for result, save output.

        Returns the output file path on success, None on failure.
        ``cancel_event`` is a ``threading.Event`` checked between polls.
        ``workflow_json`` is an optional JSON string of a ComfyUI API workflow;
        if non-empty, it is patched with the current parameters instead of
        using the built-in Flux Kontext workflow.
        """
        try:
            server_name = self._upload_image(image_path)
            seed = int(time.time()) % (2**53)
            if workflow_json:
                raw = json.loads(workflow_json)
                workflow = self._normalize_workflow(raw)
                self._patch_workflow(workflow, server_name, prompt, denoise, seed)
            else:
                workflow = self._build_workflow(server_name, prompt, denoise, seed)
            prompt_id = self._queue_prompt(workflow)
            logger.info("ComfyUI queued prompt %s for %s", prompt_id, image_path)

            result = self._poll_result(prompt_id, cancel_event, timeout)
            if result is None:
                return None

            image_data = self._download_result(result)
            if image_data is None:
                return None

            out_path = self._next_version_path(image_path)
            Path(out_path).write_bytes(image_data)
            logger.info("ComfyUI saved result to %s (%d bytes)", out_path, len(image_data))
            return out_path

        except urllib.error.URLError as e:
            logger.error("ComfyUI network error: %s", e)
            return None
        except Exception as e:
            logger.error("ComfyUI generation failed: %s", e, exc_info=True)
            return None

    # ── Internals ────────────────────────────────────────────────

    def _upload_image(self, image_path: str) -> str:
        """Multipart POST to /upload/image. Returns server-side filename."""
        path = Path(image_path)
        boundary = uuid.uuid4().hex
        filename = path.name
        mime = mimetypes.guess_type(str(path))[0] or "image/png"

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{self.base_url}/upload/image",
            data=body,
            method="POST",
        )
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        logger.debug("ComfyUI upload result: %s", result)
        return result["name"]

    @staticmethod
    def _normalize_workflow(raw: dict) -> dict:
        """Accept both ComfyUI API format and UI format; return API format.

        API format: ``{"1": {"class_type": "...", "inputs": {...}}, ...}``
        UI format:  ``{"nodes": [...], "links": [...], "version": ...}``

        Raises ``ValueError`` if the workflow is in UI format (not convertible
        without server-side node definitions).
        """
        # Detect UI format by the presence of "nodes" array.
        if "nodes" in raw and isinstance(raw["nodes"], list):
            raise ValueError(
                "Workflow is in ComfyUI UI format. "
                "Please re-export using 'Save (API Format)' in ComfyUI."
            )
        # API format — strip non-node metadata keys (e.g. "version", "extra").
        workflow = {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and "class_type" in v
        }
        if not workflow:
            raise ValueError("Workflow contains no valid nodes with 'class_type'.")
        return workflow

    @staticmethod
    def _patch_workflow(workflow: dict, server_image: str, prompt: str,
                        denoise: float, seed: int) -> None:
        """Patch a user-supplied workflow dict in-place with runtime parameters."""
        for node in workflow.values():
            ct = node.get("class_type", "")
            inputs = node.get("inputs", {})
            if ct == "LoadImage":
                inputs["image"] = server_image
            elif ct == "CLIPTextEncode":
                inputs["text"] = prompt
            elif ct == "KSampler":
                inputs["denoise"] = denoise
                inputs["seed"] = seed

    def _build_workflow(self, server_image: str, prompt: str, denoise: float, seed: int) -> dict:
        """Build the Flux Kontext workflow dict."""
        return {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": "flux1-dev-kontext_fp8_scaled.safetensors",
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "DualCLIPLoader",
                "inputs": {
                    "clip_name1": "clip_l.safetensors",
                    "clip_name2": "t5xxl_fp8_e4m3fn_scaled.safetensors",
                    "type": "flux",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "ae.safetensors"},
            },
            "4": {
                "class_type": "LoadImage",
                "inputs": {"image": server_image},
            },
            "5": {
                "class_type": "FluxKontextImageScale",
                "inputs": {"image": ["4", 0]},
            },
            "6": {
                "class_type": "VAEEncode",
                "inputs": {"pixels": ["5", 0], "vae": ["3", 0]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": prompt,
                    "clip": ["2", 0],
                },
            },
            "8": {
                "class_type": "ReferenceLatent",
                "inputs": {
                    "conditioning": ["7", 0],
                    "latent": ["6", 0],
                },
            },
            "9": {
                "class_type": "FluxGuidance",
                "inputs": {
                    "guidance": 2.5,
                    "conditioning": ["8", 0],
                },
            },
            "10": {
                "class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["7", 0]},
            },
            "11": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": 8,
                    "cfg": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": denoise,
                    "model": ["1", 0],
                    "positive": ["9", 0],
                    "negative": ["10", 0],
                    "latent_image": ["6", 0],
                },
            },
            "12": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["11", 0], "vae": ["3", 0]},
            },
            "13": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "RabbitViewer",
                    "images": ["12", 0],
                },
            },
        }

    def _queue_prompt(self, workflow: dict) -> str:
        """POST /prompt — returns prompt_id."""
        payload = json.dumps({
            "prompt": workflow,
            "client_id": self.client_id,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        return result["prompt_id"]

    def _poll_result(self, prompt_id: str, cancel_event, timeout: int) -> Optional[dict]:
        """Poll /history/{prompt_id} until complete or cancelled. Returns history entry."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                logger.info("ComfyUI generation cancelled for prompt %s", prompt_id)
                return None

            time.sleep(2)

            try:
                url = f"{self.base_url}/history/{prompt_id}"
                with urllib.request.urlopen(url, timeout=10) as resp:
                    hist = json.loads(resp.read())
            except urllib.error.URLError:
                continue

            if prompt_id not in hist:
                continue

            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("completed"):
                return entry
            if "error" in str(status).lower():
                logger.error("ComfyUI workflow error: %s", status)
                return None

        logger.error("ComfyUI generation timed out after %ds for prompt %s", timeout, prompt_id)
        return None

    def _download_result(self, entry: dict) -> Optional[bytes]:
        """Extract and download the first saved output image from a history entry.

        Prefers images with type 'output' (SaveImage) over 'temp' (PreviewImage).
        """
        outputs = entry.get("outputs", {})
        # First pass: look for saved outputs (type=output from SaveImage nodes)
        for output in outputs.values():
            for img in output.get("images", []):
                if img.get("type", "output") == "output":
                    return self._fetch_image(img)
        # Fallback: accept any image (e.g. temp previews)
        for output in outputs.values():
            for img in output.get("images", []):
                return self._fetch_image(img)
        logger.error("ComfyUI: no output images in history entry")
        return None

    def _fetch_image(self, img: dict) -> bytes:
        """Download a single image from ComfyUI /view endpoint."""
        filename = img["filename"]
        subfolder = img.get("subfolder", "")
        img_type = img.get("type", "output")
        url = (
            f"{self.base_url}/view?"
            f"filename={filename}&subfolder={subfolder}&type={img_type}"
        )
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()

    @staticmethod
    def _next_version_path(original_path: str) -> str:
        """Find next available version: stem_v1.png, stem_v2.png, etc."""
        p = Path(original_path)
        stem = p.stem
        suffix = ".png"
        parent = p.parent
        version = 1
        while True:
            candidate = parent / f"{stem}_v{version}{suffix}"
            if not candidate.exists():
                return str(candidate)
            version += 1
