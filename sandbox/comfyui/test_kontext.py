"""
Test: Send an image through the Flux Kontext Kodak Gold workflow on ComfyUI.

Uploads the source image, runs the workflow, downloads the result,
and saves it next to the original with a version suffix.
"""

import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

SERVER = "http://192.168.50.4:8188"
CLIENT_ID = str(uuid.uuid4())

SOURCE_IMAGE = Path("/Users/joe/Downloads/test/demo/0483.png")


def upload_image(filepath: Path) -> str:
    """Upload an image to ComfyUI. Returns the server-side filename."""
    import mimetypes
    boundary = uuid.uuid4().hex
    filename = filepath.name
    mime = mimetypes.guess_type(str(filepath))[0] or "image/png"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + filepath.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{SERVER}/upload/image",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    print(f"  Uploaded: {result}")
    return result["name"]


def build_workflow(image_name: str, denoise: float = 0.30) -> dict:
    """Flux Kontext Kodak Gold workflow — matches the ComfyUI graph."""
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
            "inputs": {"image": image_name},
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
                "text": (
                    "Apply a subtle Kodak Gold 200 film color grade. "
                    "Warm golden tones, slightly lifted blacks, soft grain. "
                    "professional skin retouch. caramel skin tones. "
                    "lift the tones of her eyes slightly. "
                    "give the eyes a little extra pop. "
                    "Keep everything else exactly the same."
                ),
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
                "seed": int(time.time()) % (2**53),
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
        # SaveImage instead of PreviewImage so we can retrieve it
        "13": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "RabbitViewer_test",
                "images": ["12", 0],
            },
        },
    }


def get_image(filename: str, subfolder: str = "", type_: str = "output") -> bytes:
    url = f"{SERVER}/view?filename={filename}&subfolder={subfolder}&type={type_}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def next_version_path(source: Path) -> Path:
    """Find next available version: 0483_v1.png, 0483_v2.png, etc."""
    stem = source.stem
    suffix = source.suffix
    parent = source.parent
    version = 1
    while True:
        candidate = parent / f"{stem}_v{version}{suffix}"
        if not candidate.exists():
            return candidate
        version += 1


def main():
    print(f"Source: {SOURCE_IMAGE}")
    assert SOURCE_IMAGE.exists(), f"Source image not found: {SOURCE_IMAGE}"

    out_path = next_version_path(SOURCE_IMAGE)
    print(f"Output: {out_path}")

    # 1. Upload
    print("\n[1/4] Uploading image to ComfyUI…")
    server_name = upload_image(SOURCE_IMAGE)

    # 2. Build & queue workflow
    print("[2/4] Queuing Flux Kontext workflow (denoise=0.30, 8 steps)…")
    workflow = build_workflow(server_name, denoise=0.30)
    payload = json.dumps({"prompt": workflow, "client_id": CLIENT_ID}).encode()
    req = urllib.request.Request(
        f"{SERVER}/prompt", data=payload, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    prompt_id = result["prompt_id"]
    print(f"  Queued: {prompt_id}")

    # 3. Poll for completion
    print("[3/4] Waiting for generation…")
    for tick in range(180):
        time.sleep(1)
        url = f"{SERVER}/history/{prompt_id}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            hist = json.loads(resp.read())
        if prompt_id not in hist:
            if tick % 5 == 0:
                print(f"  … waiting ({tick}s)")
            continue
        entry = hist[prompt_id]
        status = entry.get("status", {})
        if status.get("completed"):
            print(f"  Done! ({tick}s)")
            break
        if "error" in str(status).lower():
            print(f"  ERROR: {json.dumps(status, indent=2)}")
            return
    else:
        print("  Timed out after 180s.")
        return

    # 4. Download result
    print("[4/4] Downloading result…")
    outputs = entry.get("outputs", {})
    for node_id, output in outputs.items():
        for img in output.get("images", []):
            fname = img["filename"]
            subfolder = img.get("subfolder", "")
            print(f"  Fetching {fname}…")
            data = get_image(fname, subfolder)
            out_path.write_bytes(data)
            print(f"  Saved to {out_path} ({len(data):,} bytes)")
            break  # Take the first image only
        else:
            continue
        break
    else:
        print("  No output images found!")
        return

    print(f"\nDone! Result saved to: {out_path}")


if __name__ == "__main__":
    main()
