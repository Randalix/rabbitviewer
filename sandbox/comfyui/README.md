# ComfyUI Integration Sandbox

Exploratory sandbox for connecting RabbitViewer to a ComfyUI server.

## Setup

Only stdlib is required for REST. For WebSocket progress monitoring:

```bash
pip install websocket-client
```

## Usage

```bash
# Probe the server — shows GPU info, models, queue, recent history
python sandbox/comfyui/comfy_client.py probe

# Monitor WebSocket events in real time
python sandbox/comfyui/comfy_client.py monitor

# Run a txt2img generation
python sandbox/comfyui/comfy_client.py generate --prompt "a rabbit" --ckpt "model.safetensors"

# Custom host/port
python sandbox/comfyui/comfy_client.py --host 192.168.50.4 --port 8188 probe
```

## API Reference

Key ComfyUI endpoints used:

| Endpoint | Purpose |
|----------|---------|
| `GET /system_stats` | GPU, VRAM, Python version |
| `GET /models` | Available model folders/files |
| `GET /object_info` | All node types and their inputs |
| `POST /prompt` | Queue a workflow for execution |
| `GET /history/{id}` | Poll for results |
| `GET /view` | Fetch generated images |
| `WS /ws` | Real-time progress (status, step N/M, node execution) |
