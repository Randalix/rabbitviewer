"""Track recently visited directories in a standalone YAML file."""
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List

import yaml

logger = logging.getLogger(__name__)

RECENT_PATH = os.path.expanduser("~/.rabbitviewer/recent_directories.yaml")
MAX_ENTRIES = 10


@dataclass(frozen=True)
class RecentDirectory:
    path: str
    last_visited: str  # ISO-8601


def load() -> List[RecentDirectory]:
    try:
        with open(RECENT_PATH, "r") as f:  # disk-io: recent dirs load
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.debug("No recent directories file: %s", e)
        return []
    return [
        RecentDirectory(path=e["path"], last_visited=e.get("last_visited", ""))
        for e in data.get("directories", [])
        if e.get("path")
    ]


def add(directory_path: str):
    directory_path = os.path.normpath(os.path.expanduser(directory_path))
    entries = load()
    # Remove existing entry for this path (will re-add at top).
    entries = [e for e in entries if e.path != directory_path]
    entries.insert(0, RecentDirectory(
        path=directory_path,
        last_visited=datetime.now().isoformat(timespec="seconds"),
    ))
    entries = entries[:MAX_ENTRIES]
    data = {
        "directories": [
            {"path": e.path, "last_visited": e.last_visited}
            for e in entries
        ]
    }
    os.makedirs(os.path.dirname(RECENT_PATH), exist_ok=True)
    with open(RECENT_PATH, "w") as f:  # disk-io: recent dirs save
        yaml.dump(data, f, default_flow_style=False)
