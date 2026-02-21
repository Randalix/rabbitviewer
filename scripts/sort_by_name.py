from pathlib import Path


def run_script(api):
    """Sort all displayed images by filename."""
    all_images = api.get_all_images()
    if not all_images:
        return

    sorted_paths = sorted(all_images, key=lambda p: Path(p).name)
    if sorted_paths == all_images:
        return

    api.remove_images(all_images)
    api.add_images(sorted_paths)
