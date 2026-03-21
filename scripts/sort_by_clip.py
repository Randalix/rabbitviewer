"""Sort all displayed images by visual similarity using cached CLIP embeddings.

Builds a greedy nearest-neighbour chain over the L2-normalised embedding
vectors stored in the DB — no model loading or inference required.  Each step
picks the unvisited image whose cosine similarity to the current image is
highest, so adjacent images in the result share semantic visual content
(scene type, lighting, subject matter, colour).

Images without a cached embedding are placed at the end.
"""


def run_script(api):
    """Reorder displayed images by CLIP semantic similarity."""
    try:
        import numpy as np
    except ImportError:
        api.show_message("numpy is required for CLIP sort — please install it.")
        return

    all_images = api.get_all_images()
    if not all_images:
        return

    blobs = api.get_clip_embeddings(all_images)
    if not blobs:
        api.show_message(
            "No CLIP embeddings cached yet — trigger AI indexing first."
        )
        return

    # Build ordered list and matrix for images that have embeddings.
    paths_with_emb = [p for p in all_images if p in blobs]
    no_emb = [p for p in all_images if p not in blobs]

    matrix = np.stack([
        np.frombuffer(blobs[p], dtype=np.float32) for p in paths_with_emb
    ])  # shape (N, D), already L2-normalised

    n = len(paths_with_emb)
    visited = np.zeros(n, dtype=bool)
    order = []

    current = 0
    for _ in range(n):
        order.append(current)
        visited[current] = True
        # Cosine similarity of current to all images (dot product on L2-normalised vecs).
        sims = matrix @ matrix[current]
        sims[visited] = -2.0  # exclude visited; valid cosine range is [-1, 1]
        current = int(np.argmax(sims))

    sorted_paths = [paths_with_emb[i] for i in order] + no_emb
    if sorted_paths != all_images:
        api.set_image_order(sorted_paths)
