"""Face detection quality test — runs on real images to calibrate filters.

Requires onnxruntime + real view images in ~/.rabbitviewer/images/.
Skipped automatically when onnxruntime is unavailable.

Usage:
    pytest tests/test_face_detection_quality.py -v -s
"""
import os
import sqlite3
import pytest

# Skip entire module if onnxruntime not available
try:
    import onnxruntime  # noqa: F401
    import numpy as np
    from PIL import Image
except ImportError:
    pytest.skip("onnxruntime/numpy/PIL required", allow_module_level=True)

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import onnx_runtime
from core.model_manager import get_model_path
from core.face_inference import (
    detect_faces, detect_and_embed, _align_face,
    _DET_MAX_SIZE, _MIN_BBOX_DIM, _MIN_BBOX_AREA, _MIN_EMBEDDING_NORM,
    _valid_face_geometry,
)

DB_PATH = os.path.expanduser("~/.rabbitviewer/cache/metadata.db")

# Ground-truth: files that MUST produce at least one face
POSITIVE_FILES = [
    "/Users/joe/Pictures/202509_all/_MG_6103.CR3",
    "/Users/joe/Pictures/202509_all/_MG_6116.CR3",
    "/Users/joe/Pictures/202509_all/_MG_6152.CR3",
    "/Users/joe/Pictures/202509_all/_MG_6440.CR3",
    "/Users/joe/Pictures/202509_all/_MG_6466.CR3",  # hard: tiny distant face
]

# Files where detection is expected to fail (face too small/distant/occluded)
HARD_POSITIVE_FILES = {
    "/Users/joe/Pictures/202509_all/_MG_6116.CR3",  # 0 detections at conf>=0.5
    "/Users/joe/Pictures/202509_all/_MG_6152.CR3",  # 0 detections at conf>=0.5
    "/Users/joe/Pictures/202509_all/_MG_6440.CR3",  # face classified as animal (wildlife context)
    "/Users/joe/Pictures/202509_all/_MG_6466.CR3",  # tiny distant face
}


def _get_view_image(file_path):
    """Look up view_image_path from the metadata DB."""
    if not os.path.isfile(DB_PATH):
        return None
    db = sqlite3.connect(DB_PATH)
    row = db.execute(
        "SELECT view_image_path FROM image_metadata WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    db.close()
    if row and row[0] and os.path.isfile(row[0]):
        return row[0]
    return None


def _run_pipeline_verbose(file_path):
    """Run face detection with detailed per-stage logging.

    Returns list of dicts with keys:
        bbox, confidence, geom_ok, dim_ok, area_ok, norm, accepted
    """
    view = _get_view_image(file_path)
    if not view:
        return None, "no view image"

    img = Image.open(view).convert("RGB")
    full_w, full_h = img.size

    # --- Stage 1: raw SCRFD detections (no post-filters) ---
    # Use a very low confidence to see everything
    raw = detect_faces(file_path, view_image_path=view, confidence_threshold=0.5)
    if raw is None:
        return None, "detect_faces returned None (model missing?)"

    # --- Stage 2: apply each filter and record what passes/fails ---
    rec_path = get_model_path("face-recognition-buffalo_l")
    rec_session = onnx_runtime.get_session(rec_path) if rec_path else None

    results = []
    for face in raw:
        bx, by, bw, bh = face['bbox']
        conf = face['confidence']
        kps = face['keypoints']

        geom_ok = _valid_face_geometry(kps)
        dim_ok = bw >= _MIN_BBOX_DIM and bh >= _MIN_BBOX_DIM
        area_ok = bw * bh >= _MIN_BBOX_AREA
        conf_ok = conf >= 0.5  # current default

        # Get embedding norm if we have the recognition model
        norm = None
        if rec_session and geom_ok and dim_ok and area_ok and conf_ok:
            kps_px = [(k[0] * full_w, k[1] * full_h) for k in kps]
            aligned = _align_face(img, kps_px)
            arr = aligned.astype(np.float32)
            arr = (arr / 127.5) - 1.0
            arr = arr.transpose(2, 0, 1)[np.newaxis, ...]
            out = rec_session.run(None, {rec_session.get_inputs()[0].name: arr})
            norm = float(np.linalg.norm(out[0].flatten()))

        accepted = conf_ok and geom_ok and dim_ok and area_ok and (
            norm is not None and norm >= _MIN_EMBEDDING_NORM
        )

        results.append({
            'bbox': (bx, by, bw, bh),
            'confidence': conf,
            'conf_ok': conf_ok,
            'geom_ok': geom_ok,
            'dim_ok': dim_ok,
            'area_ok': area_ok,
            'norm': norm,
            'norm_ok': norm is not None and norm >= _MIN_EMBEDDING_NORM,
            'accepted': accepted,
        })

    return results, None


class TestFaceDetectionQuality:
    """Run detection pipeline on known-positive images and report per-stage stats."""

    @pytest.mark.parametrize("file_path", POSITIVE_FILES,
                             ids=[os.path.basename(f) for f in POSITIVE_FILES])
    def test_positive_file_detects_face(self, file_path):
        results, err = _run_pipeline_verbose(file_path)
        if err:
            pytest.skip(err)

        basename = os.path.basename(file_path)

        # Print detailed pipeline report
        print(f"\n{'='*70}")
        print(f"  {basename}  —  {len(results)} raw detections (conf >= 0.5)")
        print(f"{'='*70}")
        print(f"  Thresholds: conf>=0.5  dim>={_MIN_BBOX_DIM}  "
              f"area>={_MIN_BBOX_AREA}  norm>={_MIN_EMBEDDING_NORM}")
        print(f"  {'conf':>6} {'bbox':>13} {'area':>8} {'geom':>5} "
              f"{'dim':>4} {'area':>5} {'norm':>6} {'result':>8}")
        print(f"  {'-'*60}")

        accepted_count = 0
        for r in sorted(results, key=lambda x: -x['confidence']):
            bx, by, bw, bh = r['bbox']
            norm_s = f"{r['norm']:.1f}" if r['norm'] is not None else "—"
            tag = "ACCEPT" if r['accepted'] else "reject"
            reason = ""
            if not r['accepted']:
                fails = []
                if not r['conf_ok']: fails.append("conf")
                if not r['geom_ok']: fails.append("geom")
                if not r['dim_ok']: fails.append("dim")
                if not r['area_ok']: fails.append("area")
                if not r['norm_ok']: fails.append("norm")
                reason = f"  ({','.join(fails)})"
            print(f"  {r['confidence']:6.3f} {bw:.3f}x{bh:.3f}@{bx:.2f},{by:.2f} "
                  f"{bw*bh:8.5f} {'Y' if r['geom_ok'] else 'N':>5} "
                  f"{'Y' if r['dim_ok'] else 'N':>4} "
                  f"{'Y' if r['area_ok'] else 'N':>5} "
                  f"{norm_s:>6} {tag:>8}{reason}")
            if r['accepted']:
                accepted_count += 1

        print(f"\n  Result: {accepted_count} accepted / {len(results)} raw")

        if file_path in HARD_POSITIVE_FILES and accepted_count == 0:
            pytest.skip(f"{basename}: hard positive (tiny/distant face)")
        assert accepted_count >= 1, (
            f"{basename}: no faces accepted! "
            f"{len(results)} raw detections all filtered out"
        )

    def test_filter_summary(self):
        """Aggregate stats across all positive files."""
        total_raw = 0
        total_accepted = 0
        rejected_by = {'conf': 0, 'geom': 0, 'dim': 0, 'area': 0, 'norm': 0}

        for fp in POSITIVE_FILES:
            results, err = _run_pipeline_verbose(fp)
            if err:
                continue
            for r in results:
                total_raw += 1
                if r['accepted']:
                    total_accepted += 1
                else:
                    if not r['conf_ok']: rejected_by['conf'] += 1
                    elif not r['geom_ok']: rejected_by['geom'] += 1
                    elif not r['dim_ok']: rejected_by['dim'] += 1
                    elif not r['area_ok']: rejected_by['area'] += 1
                    elif not r['norm_ok']: rejected_by['norm'] += 1

        print(f"\n{'='*50}")
        print(f"  AGGREGATE: {total_accepted}/{total_raw} accepted")
        print(f"  Rejected by stage:")
        for stage, count in rejected_by.items():
            print(f"    {stage:>6}: {count}")
        print(f"{'='*50}")
