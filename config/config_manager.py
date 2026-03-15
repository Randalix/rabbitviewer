import os
import yaml

DEFAULT_CONFIG = {
    "system": {},
    "files": {
        "cache": {
            "dir": "~/.rabbitviewer/cache",
        },
    },
    "inspector": {
        "zoom_factor": 3.0,
    },
    "logging_level": "INFO",
    "logging_levels": {},
    "gui": {
        "background_color": "#000000",
        "spacing": 1,
        "border_width": 1,
        "hover_border_color": "#2d59b6",
        "select_border_color": "orange",
        "placeholder_color": "black",
        "statusbar_font": "Arial",
        "statusbar_font_size": 10,
        "monospace_font": "Menlo"
    },
    "hotkeys": {
        "escape_picture_view": {
            "sequence": "Esc",
            "description": "Return to thumbnail view"
        },
        "close_or_quit": {
            "sequence": "q",
            "description": "Close picture view, then inspector views, then quit"
        },
        "next_image": {
            "sequence": "D",
            "description": "Navigate to next image",
            "extra_sequences": ["Right"]
        },
        "previous_image": {
            "sequence": "A",
            "description": "Navigate to previous image",
            "extra_sequences": ["Left"]
        },
        "toggle_inspector": {
            "sequence": "I",
            "description": "Toggle inspector window"
        },
        "pin_inspector": {
            "sequence": "Shift+I",
            "description": "Pin/unpin inspector to current image"
        },
        "toggle_face_palette": {
            "sequence": "P",
            "description": "Open face palette"
        },
        "script:set_rating_0": {
            "sequence": "0",
            "description": "Rate selected images 0 stars"
        },
        "script:set_rating_1": {
            "sequence": "1",
            "description": "Rate selected images 1 star"
        },
        "script:set_rating_2": {
            "sequence": "2",
            "description": "Rate selected images 2 stars"
        },
        "script:set_rating_3": {
            "sequence": "3",
            "description": "Rate selected images 3 stars"
        },
        "script:set_rating_4": {
            "sequence": "4",
            "description": "Rate selected images 4 stars"
        },
        "script:set_rating_5": {
            "sequence": "5",
            "description": "Rate selected images 5 stars"
        },
        "menu:sort": {
            "sequence": "S",
            "description": "Open sort menu"
        },
        "menu:compare": {
            "sequence": "V",
            "description": "Compare selected images"
        },
        "show_hotkey_help": {
            "sequence": "?",
            "description": "Show keyboard shortcuts"
        },
        "toggle_info_panel": {
            "sequence": "M",
            "description": "Open metadata info panel"
        },
        "menu:tags": {
            "sequence": "T",
            "description": "Open tags menu"
        },
        "menu:export": {
            "sequence": "X",
            "description": "Open export menu"
        },
        "menu:rotate": {
            "sequence": "R",
            "description": "Open rotate menu"
        },
        "menu:open_with": {
            "sequence": "Space",
            "description": "Open with external application"
        },
        "menu:bookmark": {
            "sequence": "B",
            "description": "Copy/move files to bookmarked directories"
        },
        "script:delete_selected": {
            "sequence": "Shift+R",
            "description": "Delete selected images",
            "extra_sequences": ["Del"]
        },
        "open_comfyui": {
            "sequence": "G",
            "description": "Open ComfyUI generation dialog"
        },
        "zoom_in": {
            "sequence": "+",
            "description": "Zoom in"
        },
        "zoom_out": {
            "sequence": "-",
            "description": "Zoom out"
        },
        "open_filter": {
            "sequence": "Ctrl+F",
            "description": "Open filter dialog"
        },
        "undo_selection": {
            "sequence": "Ctrl+Z",
            "description": "Undo selection"
        },
        "redo_selection": {
            "sequence": "Ctrl+Shift+Z",
            "description": "Redo selection"
        },
        "toggle_group_mode": {
            "sequence": "J",
            "description": "Toggle RAW+JPG grouping"
        },
        "copy_paths": {
            "sequence": "Ctrl+Shift+C",
            "description": "Copy file paths to clipboard"
        },
        "copy_files": {
            "sequence": "Ctrl+C",
            "description": "Copy files to clipboard (for Finder paste)"
        },
        "copy_image": {
            "sequence": "Ctrl+Shift+Alt+C",
            "description": "Copy image pixels to clipboard (JPEG only)"
        },
        "clip_search": {
            "sequence": "/",
            "description": "Open CLIP semantic search"
        },
        "script:select_all": {
            "sequence": "Ctrl+A",
            "description": "Select all images"
        },
        "script:invert_selection": {
            "sequence": "Ctrl+Shift+A",
            "description": "Invert selection"
        }
    },
    "color_management": {
        "icc_profile_path": "",  # path to monitor ICC profile; empty = disabled
    },
    "ai": {
        "enabled": True,
        "clip_search": {
            "enabled": True,
            "model": "clip-vit-b-32",
            "auto_index": True,
        },
        "auto_orient": {
            "enabled": False,
            "confidence_threshold": 0.9,
        },
        "face_recognition": {
            "enabled": True,
            "auto_index": True,
            "detection_confidence": 0.5,
            "recognition_threshold": 0.6,
            "model": "buffalo_l",
        },
        "models_dir": "~/.rabbitviewer/models",
    },
    "comfyui": {
        "host": "192.168.50.4",
        "port": 8188,
        "workflows_dir": "",
    },
    "thumbnail_size": 128,
    "cache_dir": "~/.rabbitviewer",
    "watch_paths": [os.path.expanduser("~/Pictures"), os.path.expanduser("~/Downloads")],
    "remote_paths": [],  # path prefixes treated as network mounts (timeout-probed)
    "min_file_size": 8192,  # bytes; 8 KB floor
    "ignore_patterns": ["._*"],  # glob patterns
    "max_cache_size_mb": 10240,  # 10 GB; 0 = unlimited
    "fullres_cache_threshold_ms": 500,  # Below this → RAM only; above → disk
    "fullres_mem_cache_mb": 512,        # Max daemon-side memory for fast fullres images
    "metadata": {
        "default_write_mode": "sidecar",   # "sidecar" or "embedded"
        "format_write_mode": {".jpg": "embedded", ".cr3": "embedded"},
    },
}

def _default_config_path() -> str:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(xdg_config_home, "rabbitviewer", "config.yaml")


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or _default_config_path()
        self.config = self.load_config()

    def load_config(self):
        try:
            with open(self.config_path, "r") as f:  # disk-io: config load
                user_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.save_config(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed config at {self.config_path}") from exc
        return _deep_merge(DEFAULT_CONFIG, user_config)

    def save_config(self, config):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:  # disk-io: config save
            yaml.dump(config, f, default_flow_style=False)

    def get(self, key, default=None):
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default

    def set(self, key, value):
        keys = key.split('.')
        node = self.config
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        self.save_config(self.config)

    @property
    def logging_level(self):
        return self.get("logging_level", "INFO")
