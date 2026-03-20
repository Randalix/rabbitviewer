import sys
from pathlib import Path

def hamming_distance(hash1, hash2):
    """Calculate the Hamming distance between two integer hashes."""
    if hash1 is None or hash2 is None:
        return sys.maxsize
    return bin(hash1 ^ hash2).count('1')

def run_script(api):
    """Sort all displayed images by phash similarity to the selected image."""
    selected_images = api.get_selected_images()
    if not selected_images:
        api.show_message("Please select an image first to sort by similarity.")
        return

    # If multiple images are selected, use the first one as the reference.
    reference_image_path = list(selected_images)[0]
    
    all_images = api.get_all_images()
    if not all_images:
        return

    # Get metadata for all images, including the reference image
    all_metadata = api.get_metadata_batch(all_images)

    reference_metadata = all_metadata.get(reference_image_path)
    if not reference_metadata or 'phash' not in reference_metadata or reference_metadata['phash'] is None:
        api.show_message(f"The selected image '{Path(reference_image_path).name}' has no pHash value. Cannot sort by similarity.")
        return

    reference_phash = reference_metadata['phash']

    # Create a dictionary to hold paths and their phashes for sorting
    phash_dict = {path: all_metadata.get(path, {}).get('phash') for path in all_images}

    # Sort images based on the hamming distance to the reference image's phash.
    # Images without a phash will have a large distance and be placed at the end.
    sorted_paths = sorted(
        all_images,
        key=lambda path: hamming_distance(reference_phash, phash_dict[path])
    )

    if sorted_paths != all_images:
        api.set_image_order(sorted_paths)
