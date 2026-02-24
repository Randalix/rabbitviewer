import os


def run_script(api):
    """Sort all displayed images by file extension, then by name."""
    paths = api.get_all_images()
    if not paths:
        return
    sorted_paths = sorted(paths, key=lambda p: (os.path.splitext(p)[1].lower(), os.path.basename(p).lower()))
    if sorted_paths != paths:
        api.set_image_order(sorted_paths)
