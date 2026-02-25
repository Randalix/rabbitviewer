def run_script(api):
    paths = api.get_all_images()
    if not paths:
        return

    metadata = api.get_metadata_batch(paths)

    def size_key(p):
        meta = metadata.get(p, {}) if metadata else {}
        return meta.get("file_size") or 0

    sorted_paths = sorted(paths, key=size_key)
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
