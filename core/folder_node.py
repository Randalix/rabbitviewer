# core/folder_node.py
"""Qt-free dataclass for folder representation in the grid."""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class FolderNode:
    """A directory entry displayed as a card in the thumbnail grid.

    Populated from MetadataDatabase — no filesystem hits in the GUI path.
    """
    path: str                                   # absolute directory path
    name: str                                   # basename
    parent_path: Optional[str] = None           # parent directory (None for root)
    image_count: int = 0                        # direct children count
    recursive_count: int = 0                    # including subdirectories
    preview_paths: List[str] = field(default_factory=list)  # up to 4 thumbnail paths
    image_paths: List[str] = field(default_factory=list)    # all images sorted by name
    rating: int = 0                                         # star rating (0-5)
