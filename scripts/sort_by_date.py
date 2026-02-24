import os


def run_script(api):
    paths = api.get_all_images()
    if not paths:
        return

    metadata = api.get_metadata_batch(paths)

    def date_key(p):
        meta = metadata.get(p, {}) if metadata else {}
        ts = meta.get("date_taken") or meta.get("mtime")
        if ts:
            return ts
        # why: fallback to stat only when daemon has no cached timestamp
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0

    sorted_paths = sorted(paths, key=date_key)
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
