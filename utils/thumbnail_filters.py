import fnmatch
from pathlib import Path
from typing import Union

_pattern_cache = {}  # cardinality bounded by user input; no eviction needed

def matches_filter(file_path: Union[str, Path], filter_text: str) -> bool:
    """Check if file_path matches filter_text using cached fnmatch patterns.

    Case-insensitive, space-separated patterns with auto-wildcards.
    """
    if not filter_text or not filter_text.strip():
        return True

    cache_key = filter_text.strip().lower()

    if cache_key not in _pattern_cache:
        patterns = []
        for pattern in cache_key.split():
            if '*' not in pattern and '?' not in pattern:
                pattern = f"*{pattern}*"
            patterns.append(pattern)
        _pattern_cache[cache_key] = patterns

    filename = Path(file_path).name.lower()

    return any(fnmatch.fnmatch(filename, pattern)
               for pattern in _pattern_cache[cache_key])
