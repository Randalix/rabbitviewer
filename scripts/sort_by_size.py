import os


def run_script(api):
    paths = api.get_all_images()
    if not paths:
        return

    metadata = api.get_metadata_batch(paths)

    def size_key(p):
        meta = metadata.get(p, {}) if metadata else {}
        size = meta.get("file_size")
        if size is not None:
            return size
        # why: fallback to stat only when daemon has no cached file size
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    sorted_paths = sorted(paths, key=size_key)
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
