# core/file_grouping.py
"""RAW+JPG grouping: pure-function module (no Qt, no daemon deps).

When group mode is active, RAW files that share a filename stem with a JPG
are hidden from the thumbnail grid.  The JPG becomes the visible "primary"
and operations on it can be expanded to include the paired RAW files.
"""
import os
from dataclasses import dataclass
from collections import defaultdict
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple

JPG_EXTENSIONS: FrozenSet[str] = frozenset({'.jpg', '.jpeg'})

# RAW extensions collected from CR3Plugin + RawPlugin.  Hardcoded here so
# this module stays import-free (no plugin registry dependency).  New RAW
# plugins are rare; update this set when one is added.
RAW_EXTENSIONS: FrozenSet[str] = frozenset({
    '.cr3',
    '.nef', '.nrw', '.arw', '.sr2', '.srf', '.dng', '.raf', '.orf',
    '.rw2', '.pef', '.srw', '.mrw', '.rwl', '.3fr', '.fff', '.mef',
    '.mos', '.iiq', '.cap', '.eip', '.cr2',
})


@dataclass(frozen=True)
class FileGroup:
    """A RAW+JPG pairing.  *primary* is the JPG shown in the grid."""
    primary: str
    raw_files: Tuple[str, ...]

    @property
    def all_files(self) -> Tuple[str, ...]:
        return (self.primary,) + self.raw_files


def build_group_map(
    file_paths: Iterable[str],
) -> Tuple[Dict[str, FileGroup], Set[str]]:
    """Build a mapping from JPG primary path → FileGroup.

    Returns ``(group_map, hidden_raw_paths)`` where *hidden_raw_paths* is
    the set of RAW paths that should be hidden when group mode is active.
    """
    # Group by (directory, stem) — files in different directories with the
    # same stem are NOT grouped together.
    buckets: Dict[Tuple[str, str], Dict[str, List[str]]] = defaultdict(
        lambda: {"jpg": [], "raw": []}
    )

    for path in file_paths:
        ext = os.path.splitext(path)[1].lower()
        if ext in JPG_EXTENSIONS:
            dirn = os.path.dirname(path)
            stem = os.path.splitext(os.path.basename(path))[0]
            buckets[(dirn, stem)]["jpg"].append(path)
        elif ext in RAW_EXTENSIONS:
            dirn = os.path.dirname(path)
            stem = os.path.splitext(os.path.basename(path))[0]
            buckets[(dirn, stem)]["raw"].append(path)

    group_map: Dict[str, FileGroup] = {}
    hidden_raw_paths: Set[str] = set()

    for members in buckets.values():
        jpgs = members["jpg"]
        raws = members["raw"]
        if jpgs and raws:
            # Use the first JPG as primary (typically only one).
            primary = jpgs[0]
            raw_tuple = tuple(sorted(raws))
            group_map[primary] = FileGroup(primary=primary, raw_files=raw_tuple)
            hidden_raw_paths.update(raws)

    return group_map, hidden_raw_paths


def expand_paths_for_group(
    paths: Iterable[str],
    group_map: Dict[str, FileGroup],
) -> List[str]:
    """Expand primary paths to include their paired RAW files."""
    result = list(paths)
    seen: Set[str] = set(result)
    for path in result[:]:  # iterate snapshot; result may grow
        group = group_map.get(path)
        if group:
            for raw in group.raw_files:
                if raw not in seen:
                    seen.add(raw)
                    result.append(raw)
    return result
