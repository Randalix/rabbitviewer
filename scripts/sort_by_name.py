from pathlib import Path


def run_script(api):
    """Sort all displayed images by filename."""
    paths = api.get_all_images()
    if not paths:
        return
    sorted_paths = sorted(paths, key=lambda p: Path(p).name.lower())
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
