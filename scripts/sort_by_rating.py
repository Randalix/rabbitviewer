import logging


def run_script(api):
    paths = api.get_all_images()
    if not paths:
        return

    metadata = api.get_metadata_batch(paths)
    if not metadata:
        logging.warning("sort_by_rating: no metadata available")
        return

    def rating_key(p):
        meta = metadata.get(p, {})
        return -(meta.get("rating", 0) or 0)

    sorted_paths = sorted(paths, key=rating_key)
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
