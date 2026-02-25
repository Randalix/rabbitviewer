import os
import yaml

DEFAULT_CONFIG = {
    "system": {
        "socket_path": f"/tmp/rabbitviewer_{os.getenv('USER', 'user')}.sock",
    },
    "files": {
        "cache": {
            "dir": "~/.rabbitviewer/cache",
        },
    },
    "inspector": {
        "zoom_factor": 3.0,
    },
    "logging_level": "DEBUG",
    "gui": {
        "background_color": "#000000",
        "spacing": 1,
        "border_width": 1,
        "hover_border_color": "#2d59b6",
        "select_border_color": "orange",
        "placeholder_color": "black",
        "statusbar_font": "Arial",
        "statusbar_font_size": 10
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
        "start_range_selection": {
            "sequence": "S",
            "description": "Toggle range selection mode"
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
            "sequence": "F",
            "description": "Open sort menu"
        },
        "show_hotkey_help": {
            "sequence": "?",
            "description": "Show keyboard shortcuts"
        },
        "toggle_info_panel": {
            "sequence": "M",
            "description": "Open metadata info panel"
        }
    },
    "thumbnail_size": 128,
    "cache_dir": "~/.rabbitviewer",
    "watch_paths": [os.path.expanduser("~/Pictures"), os.path.expanduser("~/Downloads")],
    "min_file_size": 8192,  # bytes; 8 KB floor
    "ignore_patterns": ["._*"]  # glob patterns
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
            with open(self.config_path, "r") as f:
                user_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.save_config(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed config at {self.config_path}") from exc
        return _deep_merge(DEFAULT_CONFIG, user_config)

    def save_config(self, config):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
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
