"""
Thumbnail generation benchmark.

Usage:
    python bench_thumbnail.py /path/to/folder [--count N] [--clear-cache]

Measures per-file timing for:
  - File header read (hash + prefetch buffer)
  - Orientation extraction from buffer
  - IFD1 thumbnail extraction from buffer (hit/miss)
  - Exiftool fallback calls
  - PIL generation + save
  - Total end-to-end
"""
import argparse
import glob
import hashlib
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
from statistics import mean, median, stdev
from typing import List, Optional, Tuple

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Minimal standalone versions of the key operations (no daemon required)
# ---------------------------------------------------------------------------

PREFETCH_SIZE = 512 * 1024  # 512 KB


def read_file_header(path: str) -> Tuple[Optional[str], bytes, float]:
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        header = f.read(PREFETCH_SIZE)
    elapsed = time.perf_counter() - t0
    md5 = hashlib.md5(header[:256 * 1024]).hexdigest()
    return md5, header, elapsed


def get_orientation_from_buffer(buffer: bytes) -> int:
    sig = b'\x12\x01\x03\x00\x01\x00\x00\x00'
    pos = buffer.find(sig)
    if pos != -1:
        try:
            return struct.unpack('<H', buffer[pos + 8: pos + 10])[0]
        except struct.error:
            pass
    return 1


_CANON_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')


def extract_thumbnail_from_buffer(buffer: bytes) -> Optional[bytes]:
    """
    Locate the thumbnail JPEG inside the Canon uuid box (ISOBMFF-aware).
    CR3 files embed thumbnails in a proprietary Canon uuid box, not in EXIF IFD1.
    NOTE: This is a standalone copy of the logic in plugins/cr3_plugin.py
    (_extract_thumbnail_from_buffer). Keep in sync if the algorithm changes.
    """
    try:
        pos = 0
        n = len(buffer)
        while pos + 8 <= n:
            box_size = struct.unpack_from('>I', buffer, pos)[0]
            box_type = buffer[pos + 4: pos + 8]
            if box_size < 8:
                break
            if box_type == b'moov':
                moov_end = min(pos + box_size, n)
                inner = pos + 8
                while inner + 24 <= moov_end:
                    isz  = struct.unpack_from('>I', buffer, inner)[0]
                    ityp = buffer[inner + 4: inner + 8]
                    if isz < 8:
                        break
                    if ityp == b'uuid' and buffer[inner + 8: inner + 24] == _CANON_UUID:
                        content_start = inner + 24
                        content_end   = min(inner + isz, n)
                        # Skip Canon-proprietary blocks (SOI + SOF) to find the
                        # real thumbnail JPEG (SOI + DQT/APPn).
                        search_pos = content_start
                        soi = -1
                        while search_pos < content_end - 3:
                            p = buffer.find(b'\xff\xd8\xff', search_pos, content_end)
                            if p == -1:
                                break
                            fourth = buffer[p + 3]
                            if fourth == 0xDB or 0xE0 <= fourth <= 0xEF:
                                soi = p
                                break
                            search_pos = p + 3
                        if soi == -1:
                            return None
                        eoi = buffer.find(b'\xff\xd9', soi + 2, content_end)
                        if eoi == -1:
                            return None
                        return buffer[soi: eoi + 2]
                    inner += isz
                break
            pos += box_size
    except struct.error:
        pass
    return None


def make_stay_open_pool():
    """Return an ExifToolProcess from the daemon's plugin module (stay_open)."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from plugins.exiftool_process import ExifToolProcess
    return ExifToolProcess()


def extract_thumbnail_exiftool(path: str, pool=None) -> Tuple[Optional[bytes], float]:
    t0 = time.perf_counter()
    try:
        if pool is not None:
            data = pool.execute(["-ThumbnailImage", "-b", path])
        else:
            import subprocess
            r = subprocess.run(
                ["exiftool", "-ThumbnailImage", "-b", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=30,
            )
            data = r.stdout if r.stdout else None
        elapsed = time.perf_counter() - t0
        return data, elapsed
    except Exception:
        return None, time.perf_counter() - t0


def generate_thumbnail_pil(jpeg_bytes: bytes, orientation: int,
                            out_path: str, size: int = 256) -> float:
    from PIL import Image
    t0 = time.perf_counter()
    import io as _io
    img = Image.open(_io.BytesIO(jpeg_bytes))
    ops = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
           4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
           6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
           8: Image.Transpose.ROTATE_90}
    if orientation in ops:
        img = img.transpose(ops[orientation])
    if img.width > size or img.height > size:
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
    img.save(out_path, "JPEG", quality=85)
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--count", type=int, default=20,
                    help="Max number of files to benchmark (default: 20)")
    args = ap.parse_args()

    try:
        files = (
            glob.glob(os.path.join(args.folder, "*.CR3")) +
            glob.glob(os.path.join(args.folder, "*.cr3"))
        )
    except OSError:
        files = []
    if not files:
        # Fallback: use os.scandir which handles partial NAS failures better
        try:
            files = [
                os.path.join(args.folder, e.name)
                for e in os.scandir(args.folder)
                if e.name.lower().endswith((".cr3",))
            ]
        except OSError as exc:
            print(f"Cannot list {args.folder}: {exc}")
            sys.exit(1)
    if not files:
        print(f"No CR3 files found in {args.folder}")
        sys.exit(1)

    files = sorted(files)[: args.count]
    print(f"Benchmarking {len(files)} CR3 files from {args.folder}\n")

    tmp_dir = tempfile.mkdtemp(prefix="rabbit_bench_")
    # Warm up a single stay_open exiftool process for fallback timing.
    et_pool = make_stay_open_pool()
    et_pool.execute(["-ThumbnailImage", "-b", files[0]])   # warmup call

    try:
        results = []
        buffer_hits = 0

        for i, path in enumerate(files):
            name = os.path.basename(path)
            out_thumb = os.path.join(tmp_dir, f"thumb_{i}.jpg")

            # --- 1. read header (one NAS round-trip) ---
            _md5, header, read_elapsed = read_file_header(path)

            # --- 2. orientation from buffer (free) ---
            t0 = time.perf_counter()
            orientation = get_orientation_from_buffer(header)
            orient_elapsed = time.perf_counter() - t0

            # --- 3. thumbnail from buffer ---
            t0 = time.perf_counter()
            thumb_bytes = extract_thumbnail_from_buffer(header)
            thumb_from_buf_elapsed = time.perf_counter() - t0
            exiftool_elapsed = 0.0

            if thumb_bytes is not None:
                buffer_hits += 1
                source = "buffer"
            else:
                # Fallback uses stay_open pool (realistic app cost, not cold-start)
                thumb_bytes, exiftool_elapsed = extract_thumbnail_exiftool(path, pool=et_pool)
                source = "stay_open" if thumb_bytes else "FAILED"

            # --- 4. PIL save ---
            pil_elapsed = 0.0
            if thumb_bytes:
                pil_elapsed = generate_thumbnail_pil(thumb_bytes, orientation, out_thumb)

            total = read_elapsed + orient_elapsed + thumb_from_buf_elapsed + exiftool_elapsed + pil_elapsed

            results.append({
                "name": name,
                "read": read_elapsed,
                "orient": orient_elapsed,
                "buf_extract": thumb_from_buf_elapsed,
                "exiftool": exiftool_elapsed,
                "pil": pil_elapsed,
                "total": total,
                "source": source,
                "orientation": orientation,
            })

            print(f"[{i+1:2d}/{len(files)}] {name:40s}  "
                  f"read={read_elapsed*1000:6.1f}ms  "
                  f"buf={thumb_from_buf_elapsed*1000:5.2f}ms  "
                  f"et={exiftool_elapsed*1000:6.1f}ms  "
                  f"pil={pil_elapsed*1000:5.1f}ms  "
                  f"total={total*1000:6.1f}ms  [{source}]")

        totals = [r["total"] for r in results]
        reads  = [r["read"]  for r in results]
        print(f"""
Summary ({len(results)} files):
  Buffer hits : {buffer_hits}/{len(results)} ({100*buffer_hits//len(results)}%)
  Total/file  : mean={mean(totals)*1000:.1f}ms  median={median(totals)*1000:.1f}ms  stdev={stdev(totals)*1000:.1f}ms  max={max(totals)*1000:.1f}ms
  NAS read    : mean={mean(reads)*1000:.1f}ms  median={median(reads)*1000:.1f}ms  max={max(reads)*1000:.1f}ms
  Throughput  : ~{len(results)/sum(totals):.1f} files/s single-thread  (~{8*len(results)/sum(totals):.0f} with 8 workers)
""")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
