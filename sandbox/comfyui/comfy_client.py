"""
Sandbox client for exploring ComfyUI integration.

Usage:
    python sandbox/comfyui/comfy_client.py [--host HOST] [--port PORT]

Connects to a ComfyUI server and exercises the main API endpoints.
"""

import argparse
import json
import urllib.request
import urllib.error
import uuid
import threading
import time
from pathlib import Path

# websocket-client is optional — install with: pip install websocket-client
try:
    import websocket  # websocket-client package
    HAS_WS = True
except ImportError:
    HAS_WS = False


class ComfyClient:
    """Minimal ComfyUI API client (stdlib only for REST, optional websocket)."""

    def __init__(self, host: str = "192.168.50.4", port: int = 8188):
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.client_id = str(uuid.uuid4())
        self._ws = None
        self._ws_thread = None

    # ── REST helpers ──────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict | list | bytes:
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url += f"?{qs}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type or "text" in content_type:
                return json.loads(data)
            return data

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    # ── Info endpoints ────────────────────────────────────────────

    def system_stats(self) -> dict:
        """GET /system_stats — Python version, GPU devices, VRAM."""
        return self._get("/system_stats")

    def object_info(self, node_class: str | None = None) -> dict:
        """GET /object_info — all node types, or a single one."""
        path = f"/object_info/{node_class}" if node_class else "/object_info"
        return self._get(path)

    def list_models(self, folder: str | None = None) -> list | dict:
        """GET /models — list model folders or models in a folder."""
        path = f"/models/{folder}" if folder else "/models"
        return self._get(path)

    def embeddings(self) -> list:
        return self._get("/embeddings")

    def queue_status(self) -> dict:
        """GET /queue — pending and running items."""
        return self._get("/queue")

    def history(self, prompt_id: str | None = None) -> dict:
        path = f"/history/{prompt_id}" if prompt_id else "/history"
        return self._get(path)

    # ── Workflow execution ────────────────────────────────────────

    def queue_prompt(self, workflow: dict) -> dict:
        """POST /prompt — enqueue a workflow. Returns prompt_id + number."""
        payload = {
            "prompt": workflow,
            "client_id": self.client_id,
        }
        return self._post("/prompt", payload)

    def interrupt(self):
        """POST /interrupt — stop current execution."""
        return self._post("/interrupt", {})

    # ── Image retrieval ───────────────────────────────────────────

    def get_image(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        """GET /view — fetch a generated image."""
        return self._get("/view", {
            "filename": filename,
            "subfolder": subfolder,
            "type": type_,
        })

    # ── WebSocket (optional) ──────────────────────────────────────

    def connect_ws(self, on_message=None):
        """Open a persistent WebSocket for real-time progress updates."""
        if not HAS_WS:
            print("[ws] websocket-client not installed, skipping.")
            return

        url = f"{self.ws_url}?clientId={self.client_id}"

        def _default_handler(ws, raw):
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                print(f"[ws] binary frame ({len(raw)} bytes)")
                return
            msg_type = msg.get("type", "unknown")
            data = msg.get("data", {})
            if msg_type == "status":
                q = data.get("status", {}).get("exec_info", {})
                print(f"[ws] status — queue remaining: {q.get('queue_remaining', '?')}")
            elif msg_type == "progress":
                print(f"[ws] progress — step {data.get('value')}/{data.get('max')}")
            elif msg_type == "executing":
                node = data.get("node")
                if node is None:
                    print(f"[ws] execution complete (prompt {data.get('prompt_id', '?')[:8]}…)")
                else:
                    print(f"[ws] executing node: {node}")
            elif msg_type == "executed":
                print(f"[ws] executed node {data.get('node')} — outputs: {list(data.get('output', {}).keys())}")
            else:
                print(f"[ws] {msg_type}: {json.dumps(data, indent=2)[:200]}")

        handler = on_message or _default_handler

        def _on_error(ws, err):
            print(f"[ws] error: {err}")

        def _on_close(ws, code, reason):
            print(f"[ws] closed ({code}: {reason})")

        def _on_open(ws):
            print(f"[ws] connected (client_id={self.client_id[:8]}…)")

        self._ws = websocket.WebSocketApp(
            url,
            on_message=handler,
            on_error=_on_error,
            on_close=_on_close,
            on_open=_on_open,
        )
        self._ws_thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._ws_thread.start()

    def disconnect_ws(self):
        if self._ws:
            self._ws.close()
            self._ws = None


# ── Sample workflows ──────────────────────────────────────────────

def make_txt2img_workflow(
    prompt: str = "a fluffy rabbit in a meadow, golden hour, photorealistic",
    negative: str = "blurry, low quality",
    seed: int | None = None,
    steps: int = 20,
    cfg: float = 7.0,
    width: int = 512,
    height: int = 512,
    ckpt: str = "v1-5-pruned-emaonly.safetensors",
) -> dict:
    """Build a minimal txt2img workflow (API format).

    This is the raw node-graph dict that ComfyUI /prompt expects.
    Adjust `ckpt` to whatever checkpoint is available on the server.
    """
    if seed is None:
        seed = int(time.time()) % (2**32)

    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": cfg,
                "denoise": 1.0,
                "latent_image": ["5", 0],
                "model": ["4", 0],
                "negative": ["7", 0],
                "positive": ["6", 0],
                "sampler_name": "euler",
                "scheduler": "normal",
                "seed": seed,
                "steps": steps,
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1, "height": height, "width": width},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": prompt},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": negative},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "RabbitViewer", "images": ["8", 0]},
        },
    }


# ── CLI ───────────────────────────────────────────────────────────

def probe(client: ComfyClient):
    """Run a quick probe of the server to see what's available."""
    print("=" * 60)
    print(f"Probing ComfyUI at {client.base_url}")
    print("=" * 60)

    # System info
    try:
        stats = client.system_stats()
        print(f"\n── System Stats ──")
        py = stats.get("system", {}).get("python_version", "?")
        print(f"  Python: {py}")
        for dev in stats.get("devices", []):
            name = dev.get("name", "?")
            vram = dev.get("vram_total", 0) / (1024**3)
            vram_free = dev.get("vram_free", 0) / (1024**3)
            print(f"  GPU: {name}  VRAM: {vram:.1f} GB ({vram_free:.1f} GB free)")
    except Exception as e:
        print(f"\n[!] system_stats failed: {e}")
        return

    # Models
    try:
        folders = client.list_models()
        print(f"\n── Model Folders ({len(folders)}) ──")
        for f in folders[:20]:
            try:
                models = client.list_models(f)
                print(f"  {f}: {len(models)} model(s)")
                for m in models[:5]:
                    name = m if isinstance(m, str) else m.get("name", m)
                    print(f"    - {name}")
                if len(models) > 5:
                    print(f"    … and {len(models) - 5} more")
            except Exception:
                print(f"  {f}: (error listing)")
    except Exception as e:
        print(f"\n[!] list_models failed: {e}")

    # Queue
    try:
        q = client.queue_status()
        running = len(q.get("queue_running", []))
        pending = len(q.get("queue_pending", []))
        print(f"\n── Queue ──")
        print(f"  Running: {running}  Pending: {pending}")
    except Exception as e:
        print(f"\n[!] queue_status failed: {e}")

    # History (last 3)
    try:
        hist = client.history()
        prompt_ids = list(hist.keys())[-3:]
        print(f"\n── Recent History ({len(hist)} total, showing last {len(prompt_ids)}) ──")
        for pid in prompt_ids:
            entry = hist[pid]
            status = entry.get("status", {})
            completed = status.get("completed", False)
            outputs = entry.get("outputs", {})
            image_count = sum(
                len(v.get("images", []))
                for v in outputs.values()
            )
            print(f"  {pid[:12]}… completed={completed} images={image_count}")
    except Exception as e:
        print(f"\n[!] history failed: {e}")

    print("\n" + "=" * 60)
    print("Probe complete. Server is reachable and responding.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="ComfyUI sandbox client")
    parser.add_argument("--host", default="192.168.50.4")
    parser.add_argument("--port", type=int, default=8188)
    sub = parser.add_subparsers(dest="command", help="sub-command")

    # probe
    sub.add_parser("probe", help="Probe server capabilities")

    # generate
    gen = sub.add_parser("generate", help="Run a txt2img generation")
    gen.add_argument("--prompt", default="a fluffy rabbit in a meadow, golden hour, photorealistic")
    gen.add_argument("--negative", default="blurry, low quality")
    gen.add_argument("--steps", type=int, default=20)
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--ckpt", default="v1-5-pruned-emaonly.safetensors")
    gen.add_argument("--output", default="sandbox/comfyui/output.png", help="Save result here")

    # ws-monitor
    sub.add_parser("monitor", help="Connect WebSocket and print events")

    args = parser.parse_args()
    client = ComfyClient(args.host, args.port)

    if args.command == "probe" or args.command is None:
        probe(client)

    elif args.command == "generate":
        workflow = make_txt2img_workflow(
            prompt=args.prompt,
            negative=args.negative,
            steps=args.steps,
            seed=args.seed,
            ckpt=args.ckpt,
        )
        print(f"Queuing txt2img: {args.prompt[:60]}…")

        # Connect WS for progress
        client.connect_ws()
        time.sleep(0.5)

        result = client.queue_prompt(workflow)
        prompt_id = result.get("prompt_id")
        print(f"Queued: prompt_id={prompt_id}")

        # Poll for completion
        for _ in range(120):
            time.sleep(1)
            hist = client.history(prompt_id)
            if prompt_id in hist:
                entry = hist[prompt_id]
                if entry.get("status", {}).get("completed"):
                    print("Generation complete!")
                    # Find output images
                    for node_id, output in entry.get("outputs", {}).items():
                        for img in output.get("images", []):
                            fname = img["filename"]
                            subfolder = img.get("subfolder", "")
                            print(f"  Fetching {fname}…")
                            data = client.get_image(fname, subfolder)
                            out_path = Path(args.output)
                            out_path.parent.mkdir(parents=True, exist_ok=True)
                            out_path.write_bytes(data)
                            print(f"  Saved to {out_path} ({len(data)} bytes)")
                    break
                status_msg = entry.get("status", {}).get("status_str", "")
                if "error" in str(status_msg).lower():
                    print(f"Error: {entry.get('status')}")
                    break
        else:
            print("Timed out waiting for generation.")

        client.disconnect_ws()

    elif args.command == "monitor":
        print(f"Monitoring WebSocket at {client.ws_url}…  (Ctrl+C to stop)")
        client.connect_ws()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nDisconnecting…")
            client.disconnect_ws()


if __name__ == "__main__":
    main()
