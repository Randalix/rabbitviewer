"""
Speed benchmarks for the dynamic fullres caching system.

Measures:
  - Memory cache put/get/eviction vs disk read
  - Binary framing overhead vs JSON framing
  - Unix socket round-trip: binary vs JSON vs direct path
  - End-to-end _process_view_image_task: fast (→ RAM) vs slow (→ disk)

Run with:
    pytest tests/test_fullres_cache_bench.py -v
"""

import json
import os
import socket
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import pytest
from PIL import Image

from network._framing import FRAME_JSON, FRAME_BINARY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bench(fn, iterations=100):
    """Run *fn* for *iterations* and return timing stats (ms)."""
    timings = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - t0) * 1000)
    return {
        "iterations": iterations,
        "total_ms": sum(timings),
        "mean_ms": sum(timings) / len(timings),
        "min_ms": min(timings),
        "max_ms": max(timings),
        "p50_ms": sorted(timings)[len(timings) // 2],
    }


def _make_jpeg_bytes(width=1920, height=1080, quality=95):
    """Generate a realistic JPEG image in memory, return its bytes."""
    import io
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jpeg_bytes_small():
    """~50 KB JPEG (640x480, low quality)."""
    return _make_jpeg_bytes(640, 480, quality=60)


@pytest.fixture(scope="module")
def jpeg_bytes_large():
    """~2-5 MB JPEG (4000x3000, high quality) — typical fullres from a camera."""
    return _make_jpeg_bytes(4000, 3000, quality=95)


@pytest.fixture(scope="module")
def jpeg_on_disk(tmp_path_factory, jpeg_bytes_large):
    """Large JPEG written to disk for disk-read comparison."""
    tmp = tmp_path_factory.mktemp("fullres_bench")
    path = str(tmp / "fullres.jpg")
    with open(path, "wb") as f:
        f.write(jpeg_bytes_large)
    return path


@pytest.fixture()
def sock_path():
    path = f"/tmp/rv_bench_{uuid.uuid4().hex[:8]}.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Benchmarks: Memory Cache Operations
# ---------------------------------------------------------------------------

class TestMemoryCachePerformance:
    """Benchmarks for OrderedDict-based LRU memory cache."""

    def test_mem_cache_put_small(self, jpeg_bytes_small):
        """Time inserting a small image into the memory cache."""
        cache = OrderedDict()
        key = "/images/test.jpg"

        def put():
            cache[key] = jpeg_bytes_small
            cache.move_to_end(key)

        stats = _bench(put, iterations=10_000)
        print(f"\n  mem_cache put (small {len(jpeg_bytes_small)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.1

    def test_mem_cache_put_large(self, jpeg_bytes_large):
        """Time inserting a large image into the memory cache."""
        cache = OrderedDict()
        key = "/images/test.jpg"

        def put():
            cache[key] = jpeg_bytes_large
            cache.move_to_end(key)

        stats = _bench(put, iterations=1000)
        print(f"\n  mem_cache put (large {len(jpeg_bytes_large)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 1

    def test_mem_cache_get_hit(self, jpeg_bytes_large):
        """Time retrieving a cached image (LRU hit)."""
        cache = OrderedDict()
        key = "/images/test.jpg"
        cache[key] = jpeg_bytes_large

        def get():
            _ = cache.get(key)

        stats = _bench(get, iterations=10_000)
        print(f"\n  mem_cache get hit ({len(jpeg_bytes_large)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.01

    def test_mem_cache_get_miss(self):
        """Time a cache miss."""
        cache = OrderedDict()

        def get():
            _ = cache.get("/images/nonexistent.jpg")

        stats = _bench(get, iterations=10_000)
        print(f"\n  mem_cache get miss: {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.01

    def test_mem_cache_eviction_pressure(self, jpeg_bytes_large):
        """Time put+evict under memory pressure (100 entries, ~500 MB cap)."""
        cache = OrderedDict()
        total_bytes = 0
        max_bytes = 50 * 1024 * 1024  # 50 MB cap for test
        counter = [0]

        def put_with_evict():
            nonlocal total_bytes
            key = f"/images/img_{counter[0]}.jpg"
            counter[0] += 1
            cache[key] = jpeg_bytes_large
            cache.move_to_end(key)
            total_bytes += len(jpeg_bytes_large)
            while total_bytes > max_bytes and cache:
                _, evicted = cache.popitem(last=False)
                total_bytes -= len(evicted)

        stats = _bench(put_with_evict, iterations=200)
        print(f"\n  mem_cache put+evict (50 MB cap, {len(jpeg_bytes_large)} bytes each): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 1

    def test_mem_cache_vs_disk_read(self, jpeg_bytes_large, jpeg_on_disk):
        """Compare memory cache retrieval vs disk file read."""
        cache = OrderedDict()
        cache["/images/test.jpg"] = jpeg_bytes_large

        stats_mem = _bench(lambda: cache.get("/images/test.jpg"), iterations=1000)
        stats_disk = _bench(lambda: open(jpeg_on_disk, "rb").read(), iterations=100)

        print(f"\n  mem cache get: {stats_mem['mean_ms']:.4f} ms")
        print(f"  disk file read ({len(jpeg_bytes_large)} bytes): {stats_disk['mean_ms']:.4f} ms")
        print(f"  speedup: {stats_disk['mean_ms'] / max(stats_mem['mean_ms'], 0.0001):.0f}x")
        # Memory should be significantly faster than disk
        assert stats_mem["mean_ms"] < stats_disk["mean_ms"]


# ---------------------------------------------------------------------------
# Benchmarks: Binary Framing
# ---------------------------------------------------------------------------

class TestBinaryFramingPerformance:
    """Benchmarks for binary vs JSON framing overhead."""

    def test_json_frame_encode(self, jpeg_bytes_small):
        """Time JSON-encoding a response with a file path (current disk-cache path)."""
        response = {"status": "success", "view_image_path": "/cache/images/abc123.jpg",
                     "view_image_source": "disk"}

        def encode():
            body = FRAME_JSON + json.dumps(response).encode()
            _ = len(body).to_bytes(4, "big") + body

        stats = _bench(encode, iterations=10_000)
        print(f"\n  JSON frame encode (path response): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.1

    def test_binary_frame_encode_small(self, jpeg_bytes_small):
        """Time binary-framing a small JPEG (~50 KB)."""
        def encode():
            body = FRAME_BINARY + jpeg_bytes_small
            _ = len(body).to_bytes(4, "big") + body

        stats = _bench(encode, iterations=1000)
        print(f"\n  binary frame encode ({len(jpeg_bytes_small)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 1

    def test_binary_frame_encode_large(self, jpeg_bytes_large):
        """Time binary-framing a large JPEG (~2-5 MB)."""
        def encode():
            body = FRAME_BINARY + jpeg_bytes_large
            _ = len(body).to_bytes(4, "big") + body

        stats = _bench(encode, iterations=100)
        print(f"\n  binary frame encode ({len(jpeg_bytes_large)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 50

    def test_binary_frame_decode(self, jpeg_bytes_large):
        """Time decoding a binary-framed response (type peek + slice)."""
        payload = FRAME_BINARY + jpeg_bytes_large

        def decode():
            frame_type = payload[0:1]
            _ = payload[1:]

        stats = _bench(decode, iterations=10_000)
        print(f"\n  binary frame decode ({len(jpeg_bytes_large)} bytes): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.1

    def test_json_frame_decode(self):
        """Time decoding a JSON-framed response (parse + validate)."""
        response = {"status": "success", "view_image_path": "/cache/images/abc123.jpg",
                     "view_image_source": "disk"}
        payload = FRAME_JSON + json.dumps(response).encode()

        def decode():
            frame_type = payload[0:1]
            _ = json.loads(payload[1:].decode())

        stats = _bench(decode, iterations=10_000)
        print(f"\n  JSON frame decode (path response): {stats['mean_ms']:.4f} ms mean")
        assert stats["mean_ms"] < 0.1


# ---------------------------------------------------------------------------
# Benchmarks: Unix Socket Round-Trip
# ---------------------------------------------------------------------------

def _serve_n_requests(sock_path, responses, ready, binary_indices=None):
    """Serve N sequential requests on the same connection.

    *responses* is a list of (frame_type_byte, payload_bytes) tuples.
    """
    binary_indices = binary_indices or set()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    ready.set()
    conn, _ = server.accept()
    try:
        for frame_type, payload in responses:
            # Read the request (consume and discard).
            length_data = conn.recv(4)
            if not length_data:
                break
            msg_len = int.from_bytes(length_data, "big")
            _consume(conn, msg_len)
            # Send framed response.
            body = frame_type + payload
            conn.sendall(len(body).to_bytes(4, "big") + body)
    finally:
        conn.close()
        server.close()


def _consume(sock, n):
    """Read exactly n bytes from sock (discard)."""
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            break
        remaining -= len(chunk)


class TestSocketRoundTripPerformance:
    """Benchmarks for JSON vs binary vs direct responses over a Unix socket."""

    def _run_roundtrip_bench(self, sock_path, responses, iterations, send_fn):
        """Helper: start server, run *iterations* send/receive calls, return stats."""
        ready = threading.Event()
        t = threading.Thread(
            target=_serve_n_requests, args=(sock_path, responses, ready))
        t.start()
        ready.wait(timeout=2)

        from network.socket_client import SocketConnection
        conn = SocketConnection(sock_path, timeout=5.0)
        try:
            timings = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                send_fn(conn)
                timings.append((time.perf_counter() - t0) * 1000)
            return {
                "iterations": iterations,
                "total_ms": sum(timings),
                "mean_ms": sum(timings) / len(timings),
                "min_ms": min(timings),
                "max_ms": max(timings),
                "p50_ms": sorted(timings)[len(timings) // 2],
            }
        finally:
            conn.close()
            t.join(timeout=5)

    def test_json_roundtrip_path_response(self, sock_path):
        """Time a full JSON round-trip returning a file path (disk-cache hit)."""
        n = 50
        response_dict = {"status": "success",
                         "view_image_path": "/cache/images/abc123.jpg",
                         "view_image_source": "disk"}
        payload = json.dumps(response_dict).encode()
        responses = [(FRAME_JSON, payload)] * n

        stats = self._run_roundtrip_bench(
            sock_path, responses, n,
            lambda conn: conn.send_receive({"command": "request_view_image"}),
        )
        print(f"\n  JSON round-trip (path response): {stats['mean_ms']:.3f} ms mean, "
              f"p50={stats['p50_ms']:.3f} ms")
        assert stats["mean_ms"] < 10

    def test_binary_roundtrip_small(self, sock_path, jpeg_bytes_small):
        """Time a binary round-trip returning a small JPEG (~50 KB)."""
        n = 50
        responses = [(FRAME_BINARY, jpeg_bytes_small)] * n

        stats = self._run_roundtrip_bench(
            sock_path, responses, n,
            lambda conn: conn.send_receive_binary({"command": "request_view_image"}),
        )
        print(f"\n  binary round-trip ({len(jpeg_bytes_small)} bytes): "
              f"{stats['mean_ms']:.3f} ms mean, p50={stats['p50_ms']:.3f} ms")
        assert stats["mean_ms"] < 10

    def test_binary_roundtrip_large(self, sock_path, jpeg_bytes_large):
        """Time a binary round-trip returning a large JPEG (~2-5 MB)."""
        n = 20
        responses = [(FRAME_BINARY, jpeg_bytes_large)] * n

        stats = self._run_roundtrip_bench(
            sock_path, responses, n,
            lambda conn: conn.send_receive_binary({"command": "request_view_image"}),
        )
        print(f"\n  binary round-trip ({len(jpeg_bytes_large)} bytes): "
              f"{stats['mean_ms']:.3f} ms mean, p50={stats['p50_ms']:.3f} ms")
        assert stats["mean_ms"] < 50

    def test_json_vs_binary_roundtrip(self, sock_path, jpeg_bytes_small):
        """Compare JSON path response vs binary image response."""
        n = 30

        # JSON path response
        json_payload = json.dumps({
            "status": "success",
            "view_image_path": "/cache/images/abc123.jpg",
            "view_image_source": "disk",
        }).encode()
        json_responses = [(FRAME_JSON, json_payload)] * n
        # Binary image response
        binary_responses = [(FRAME_BINARY, jpeg_bytes_small)] * n
        all_responses = json_responses + binary_responses

        ready = threading.Event()
        t = threading.Thread(
            target=_serve_n_requests, args=(sock_path, all_responses, ready))
        t.start()
        ready.wait(timeout=2)

        from network.socket_client import SocketConnection
        conn = SocketConnection(sock_path, timeout=5.0)
        try:
            # Measure JSON round-trips
            json_timings = []
            for _ in range(n):
                t0 = time.perf_counter()
                conn.send_receive({"command": "ping"})
                json_timings.append((time.perf_counter() - t0) * 1000)

            # Measure binary round-trips
            binary_timings = []
            for _ in range(n):
                t0 = time.perf_counter()
                conn.send_receive_binary({"command": "ping"})
                binary_timings.append((time.perf_counter() - t0) * 1000)

            json_mean = sum(json_timings) / len(json_timings)
            binary_mean = sum(binary_timings) / len(binary_timings)
            print(f"\n  JSON path round-trip: {json_mean:.3f} ms mean")
            print(f"  binary {len(jpeg_bytes_small)} bytes round-trip: {binary_mean:.3f} ms mean")
            print(f"  binary overhead vs JSON: {binary_mean - json_mean:+.3f} ms")
        finally:
            conn.close()
            t.join(timeout=5)


# ---------------------------------------------------------------------------
# Benchmarks: ThumbnailManager Integration
# ---------------------------------------------------------------------------

class TestProcessViewImagePerformance:
    """Benchmarks for _process_view_image_task fast/slow path decision."""

    @pytest.fixture()
    def tm_env(self, tmp_path):
        """ThumbnailManager with PIL plugin and a test JPEG."""
        from tests.conftest import MockConfigManager
        import core.metadata_database as _mdb_module
        from core.metadata_database import MetadataDatabase
        from core.thumbnail_manager import ThumbnailManager

        _mdb_module._metadata_database = None
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "thumbnails").mkdir()
        (cache_dir / "images").mkdir()

        db_path = str(tmp_path / "metadata.db")
        db = MetadataDatabase(db_path)

        config = MockConfigManager({
            "cache_dir": str(cache_dir),
            "thumbnail_size": 128,
            "min_file_size": 0,
            "ignore_patterns": [],
            "fullres_cache_threshold_ms": 500,
            "fullres_mem_cache_mb": 64,
        })

        tm = ThumbnailManager(config, db, num_workers=2)
        tm.load_plugins()

        # Create a test JPEG
        src = str(tmp_path / "source.jpg")
        Image.new("RGB", (1920, 1080), color=(128, 64, 32)).save(src, "JPEG", quality=95)

        yield tm, db, src, cache_dir

        tm.shutdown()
        db.close()
        _mdb_module._metadata_database = None

    def test_process_view_image_fast_path(self, tm_env):
        """PIL JPEG processing should be fast and route to mem cache."""
        tm, db, src, _ = tm_env
        import hashlib
        md5 = hashlib.md5(open(src, "rb").read()).hexdigest()

        stats = _bench(lambda: tm._process_view_image_task(src, md5), iterations=20)
        print(f"\n  _process_view_image_task (PIL JPEG 1920x1080): {stats['mean_ms']:.3f} ms mean")
        # PIL JPEG should be well under 500ms threshold
        assert stats["mean_ms"] < 500

    def test_fast_routes_to_mem_cache(self, tm_env):
        """Verify fast extraction stores in mem cache, not disk."""
        tm, db, src, cache_dir = tm_env
        import hashlib
        md5 = hashlib.md5(open(src, "rb").read()).hexdigest()

        result = tm._process_view_image_task(src, md5)
        assert result == "memory", f"Expected 'memory' sentinel, got {result!r}"
        assert tm._mem_cache_get(src) is not None, "Image not found in mem cache"

        # Verify no disk cache file was written
        cached_paths = db.get_thumbnail_paths(src)
        assert not cached_paths.get("view_image_path"), \
            "Fast extraction should not write to disk cache"

    def test_direct_source_hint(self, tm_env):
        """Natively viewable JPEG with orientation=1 returns direct hint."""
        tm, db, src, _ = tm_env
        # Ensure metadata exists with orientation=1
        db.batch_ensure_records_exist([src])
        db.conn.execute("UPDATE image_metadata SET orientation = 1 WHERE file_path = ?", (src,))
        db.conn.commit()

        result = tm.request_view_image(src)
        assert isinstance(result, str) and result.startswith("direct:"), \
            f"Expected 'direct:...' sentinel, got {result!r}"
        assert result == f"direct:{src}"

    def test_direct_vs_mem_cache_vs_disk_latency(self, tm_env):
        """Compare latency of direct hint vs mem cache lookup vs disk cache lookup."""
        tm, db, src, cache_dir = tm_env

        # Setup: ensure metadata with orientation=1
        db.batch_ensure_records_exist([src])
        db.conn.execute("UPDATE image_metadata SET orientation = 1 WHERE file_path = ?", (src,))
        db.conn.commit()

        # Benchmark direct source hint
        stats_direct = _bench(lambda: tm.request_view_image(src), iterations=200)

        # Setup: put image in mem cache and clear direct conditions
        jpeg_bytes = open(src, "rb").read()
        tm._mem_cache_put(src, jpeg_bytes)
        db.conn.execute("UPDATE image_metadata SET orientation = 6 WHERE file_path = ?", (src,))
        db.conn.commit()  # Disable direct path

        # Benchmark mem cache hit
        stats_mem = _bench(lambda: tm.request_view_image(src), iterations=200)

        # Setup: move to disk cache, clear mem cache
        tm._mem_cache_remove(src)
        disk_path = str(cache_dir / "images" / "bench_view.jpg")
        with open(disk_path, "wb") as f:
            f.write(jpeg_bytes)
        db.set_thumbnail_paths(src, view_image_path=disk_path)

        # Benchmark disk cache hit
        stats_disk = _bench(lambda: tm.request_view_image(src), iterations=200)

        print(f"\n  request_view_image latency:")
        print(f"    direct hint:   {stats_direct['mean_ms']:.4f} ms (p50={stats_direct['p50_ms']:.4f} ms)")
        print(f"    mem cache hit: {stats_mem['mean_ms']:.4f} ms (p50={stats_mem['p50_ms']:.4f} ms)")
        print(f"    disk cache hit:{stats_disk['mean_ms']:.4f} ms (p50={stats_disk['p50_ms']:.4f} ms)")


# ---------------------------------------------------------------------------
# Benchmarks: End-to-End Fullres Delivery
# ---------------------------------------------------------------------------

class TestEndToEndDelivery:
    """Measure full round-trip cost of different delivery paths."""

    def test_direct_path_load_qimage(self, jpeg_on_disk):
        """Time loading a JPEG directly via path (simulates direct hint path)."""
        # Simulate what the GUI does: read file → decode to bitmap
        def load():
            with open(jpeg_on_disk, "rb") as f:
                data = f.read()
            Image.open(jpeg_on_disk).tobytes()  # force decode

        stats = _bench(load, iterations=20)
        print(f"\n  direct path load + decode (4000x3000 JPEG): {stats['mean_ms']:.3f} ms")

    def test_mem_cached_bytes_decode(self, jpeg_bytes_large):
        """Time decoding a JPEG from in-memory bytes (simulates binary response path)."""
        import io

        def decode():
            Image.open(io.BytesIO(jpeg_bytes_large)).tobytes()  # force decode

        stats = _bench(decode, iterations=20)
        print(f"\n  mem-cached bytes decode ({len(jpeg_bytes_large)} bytes): {stats['mean_ms']:.3f} ms")

    def test_disk_cached_load(self, jpeg_on_disk):
        """Time loading from disk cache path (current default path)."""
        def load():
            with open(jpeg_on_disk, "rb") as f:
                _ = f.read()

        stats = _bench(load, iterations=100)
        print(f"\n  disk cache read (no decode): {stats['mean_ms']:.3f} ms")
        assert stats["mean_ms"] < 50


# ---------------------------------------------------------------------------
# Benchmarks: Scaling with Image Size
# ---------------------------------------------------------------------------

# Resolutions to sweep: label, (width, height), JPEG quality
_SCALING_SIZES = [
    ("640x480",    (640,   480),  85),   # ~30-50 KB
    ("1920x1080",  (1920,  1080), 90),   # ~150-300 KB
    ("3840x2160",  (3840,  2160), 92),   # ~800 KB - 1.5 MB   (4K)
    ("6000x4000",  (6000,  4000), 95),   # ~3-8 MB             (24 MP camera)
    ("8192x5464",  (8192,  5464), 95),   # ~8-20 MB            (45 MP camera)
]


@pytest.fixture(scope="module")
def scaling_images(tmp_path_factory):
    """Generate JPEG files at each resolution and return [(label, path, bytes)]."""
    import io
    tmp = tmp_path_factory.mktemp("scaling")
    results = []
    for label, (w, h), q in _SCALING_SIZES:
        img = Image.new("RGB", (w, h), color=(64, 128, 192))
        # Disk copy
        disk_path = str(tmp / f"{label}.jpg")
        img.save(disk_path, "JPEG", quality=q)
        # In-memory bytes
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        results.append((label, disk_path, buf.getvalue()))
    return results


class TestScalingWithImageSize:
    """Measure how every delivery path scales as image bytes grow."""

    # -- memory cache operations ------------------------------------------

    def test_mem_cache_put_scaling(self, scaling_images):
        """OrderedDict put across image sizes."""
        print("\n  mem_cache put scaling:")
        cache = OrderedDict()
        for label, _, jpeg_bytes in scaling_images:
            key = f"/img/{label}.jpg"
            stats = _bench(lambda k=key, b=jpeg_bytes: cache.__setitem__(k, b),
                           iterations=5000)
            size_kb = len(jpeg_bytes) / 1024
            print(f"    {label:>12s} ({size_kb:7.1f} KB): {stats['mean_ms']:.5f} ms")
        # Sanity: even the largest should be sub-millisecond
        assert stats["mean_ms"] < 1

    def test_mem_cache_get_scaling(self, scaling_images):
        """OrderedDict get (hit) across image sizes."""
        print("\n  mem_cache get scaling:")
        cache = OrderedDict()
        for label, _, jpeg_bytes in scaling_images:
            key = f"/img/{label}.jpg"
            cache[key] = jpeg_bytes
        for label, _, jpeg_bytes in scaling_images:
            key = f"/img/{label}.jpg"
            stats = _bench(lambda k=key: cache.get(k), iterations=10_000)
            size_kb = len(jpeg_bytes) / 1024
            print(f"    {label:>12s} ({size_kb:7.1f} KB): {stats['mean_ms']:.5f} ms")
        assert stats["mean_ms"] < 0.01

    # -- binary framing ---------------------------------------------------

    def test_binary_frame_scaling(self, scaling_images):
        """Binary frame encode+decode across image sizes."""
        print("\n  binary frame encode+decode scaling:")
        for label, _, jpeg_bytes in scaling_images:
            def roundtrip(b=jpeg_bytes):
                body = FRAME_BINARY + b
                frame = len(body).to_bytes(4, "big") + body
                # Decode
                payload = frame[5:]  # skip 4-byte length + 1-byte type
                return payload

            iters = max(20, 2000 // max(1, len(jpeg_bytes) // 100_000))
            stats = _bench(roundtrip, iterations=iters)
            size_kb = len(jpeg_bytes) / 1024
            print(f"    {label:>12s} ({size_kb:7.1f} KB): "
                  f"{stats['mean_ms']:.4f} ms  (p50={stats['p50_ms']:.4f} ms)")

    # -- socket round-trip ------------------------------------------------

    def test_binary_socket_scaling(self, scaling_images):
        """Binary Unix socket round-trip across image sizes."""
        from network.socket_client import SocketConnection

        print("\n  binary socket round-trip scaling:")
        for label, _, jpeg_bytes in scaling_images:
            sock_path = f"/tmp/rv_scale_{uuid.uuid4().hex[:8]}.sock"
            iters = max(10, 100 // max(1, len(jpeg_bytes) // 500_000))
            responses = [(FRAME_BINARY, jpeg_bytes)] * iters

            ready = threading.Event()
            t = threading.Thread(target=_serve_n_requests,
                                 args=(sock_path, responses, ready))
            t.start()
            ready.wait(timeout=2)

            conn = SocketConnection(sock_path, timeout=5.0)
            try:
                timings = []
                for _ in range(iters):
                    t0 = time.perf_counter()
                    conn.send_receive_binary({"command": "request_view_image"})
                    timings.append((time.perf_counter() - t0) * 1000)
                mean_ms = sum(timings) / len(timings)
                p50_ms = sorted(timings)[len(timings) // 2]
                size_kb = len(jpeg_bytes) / 1024
                throughput = (len(jpeg_bytes) / 1024 / 1024) / (mean_ms / 1000) if mean_ms > 0 else 0
                print(f"    {label:>12s} ({size_kb:7.1f} KB): "
                      f"{mean_ms:.3f} ms  (p50={p50_ms:.3f} ms, "
                      f"{throughput:.0f} MB/s)")
            finally:
                conn.close()
                t.join(timeout=5)
                try:
                    os.unlink(sock_path)
                except FileNotFoundError:
                    pass

    # -- disk I/O ---------------------------------------------------------

    def test_disk_read_scaling(self, scaling_images):
        """Raw disk read across image sizes."""
        print("\n  disk read scaling:")
        for label, disk_path, jpeg_bytes in scaling_images:
            iters = max(20, 500 // max(1, len(jpeg_bytes) // 200_000))
            stats = _bench(lambda p=disk_path: open(p, "rb").read(), iterations=iters)
            size_kb = len(jpeg_bytes) / 1024
            throughput = (len(jpeg_bytes) / 1024 / 1024) / (stats["mean_ms"] / 1000) if stats["mean_ms"] > 0 else 0
            print(f"    {label:>12s} ({size_kb:7.1f} KB): "
                  f"{stats['mean_ms']:.3f} ms  ({throughput:.0f} MB/s)")

    # -- PIL decode (simulates QImage.loadFromData) -----------------------

    def test_pil_decode_from_bytes_scaling(self, scaling_images):
        """PIL decode from in-memory bytes across image sizes."""
        import io
        print("\n  PIL decode from bytes scaling:")
        for label, _, jpeg_bytes in scaling_images:
            def decode(b=jpeg_bytes):
                Image.open(io.BytesIO(b)).load()

            iters = max(5, 50 // max(1, len(jpeg_bytes) // 500_000))
            stats = _bench(decode, iterations=iters)
            size_kb = len(jpeg_bytes) / 1024
            print(f"    {label:>12s} ({size_kb:7.1f} KB): {stats['mean_ms']:.3f} ms")

    def test_pil_decode_from_disk_scaling(self, scaling_images):
        """PIL decode from disk path across image sizes."""
        print("\n  PIL decode from disk scaling:")
        for label, disk_path, jpeg_bytes in scaling_images:
            def decode(p=disk_path):
                Image.open(p).load()

            iters = max(5, 50 // max(1, len(jpeg_bytes) // 500_000))
            stats = _bench(decode, iterations=iters)
            size_kb = len(jpeg_bytes) / 1024
            print(f"    {label:>12s} ({size_kb:7.1f} KB): {stats['mean_ms']:.3f} ms")

    # -- process_view_image_task ------------------------------------------

    def test_process_view_image_scaling(self, tmp_path):
        """Full _process_view_image_task across image sizes (PIL plugin)."""
        from tests.conftest import MockConfigManager
        import core.metadata_database as _mdb_module
        from core.metadata_database import MetadataDatabase
        from core.thumbnail_manager import ThumbnailManager
        import hashlib

        _mdb_module._metadata_database = None
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "thumbnails").mkdir()
        (cache_dir / "images").mkdir()
        db = MetadataDatabase(str(tmp_path / "metadata.db"))
        config = MockConfigManager({
            "cache_dir": str(cache_dir),
            "thumbnail_size": 128,
            "min_file_size": 0,
            "ignore_patterns": [],
            "fullres_cache_threshold_ms": 500,
            "fullres_mem_cache_mb": 256,
        })
        tm = ThumbnailManager(config, db, num_workers=2)
        tm.load_plugins()

        print("\n  _process_view_image_task scaling:")
        try:
            for label, (w, h), q in _SCALING_SIZES:
                src = str(tmp_path / f"scale_{label}.jpg")
                Image.new("RGB", (w, h), color=(64, 128, 192)).save(src, "JPEG", quality=q)
                md5 = hashlib.md5(open(src, "rb").read()).hexdigest()
                file_kb = os.path.getsize(src) / 1024

                iters = max(3, 20 // max(1, w * h // 2_000_000))
                timings = []
                for _ in range(iters):
                    # Clear caches so each iteration is a fresh generation.
                    tm._mem_cache_remove(src)
                    db.set_thumbnail_paths(src, view_image_path=None)

                    t0 = time.perf_counter()
                    result = tm._process_view_image_task(src, md5)
                    timings.append((time.perf_counter() - t0) * 1000)

                mean_ms = sum(timings) / len(timings)
                p50_ms = sorted(timings)[len(timings) // 2]
                routed = "RAM" if result == "memory" else "disk"
                print(f"    {label:>12s} ({file_kb:7.1f} KB): "
                      f"{mean_ms:.1f} ms  (p50={p50_ms:.1f} ms) → {routed}")
        finally:
            tm.shutdown()
            db.close()
            _mdb_module._metadata_database = None

    # -- summary comparison -----------------------------------------------

    def test_delivery_path_comparison(self, scaling_images):
        """Side-by-side: mem-cache-get + decode vs disk-read + decode."""
        import io
        cache = OrderedDict()
        for _, _, jpeg_bytes in scaling_images:
            cache[id(jpeg_bytes)] = jpeg_bytes

        print("\n  delivery path comparison (get + decode):")
        print(f"    {'resolution':>12s}  {'size':>9s}  {'mem→decode':>12s}  {'disk→decode':>12s}  {'speedup':>8s}")
        print(f"    {'─'*12}  {'─'*9}  {'─'*12}  {'─'*12}  {'─'*8}")

        for label, disk_path, jpeg_bytes in scaling_images:
            size_kb = len(jpeg_bytes) / 1024
            iters = max(5, 30 // max(1, len(jpeg_bytes) // 500_000))

            # Memory path: cache.get → PIL decode
            def mem_path(b=jpeg_bytes):
                data = cache.get(id(b))
                Image.open(io.BytesIO(data)).load()

            # Disk path: file read → PIL decode
            def disk_path_fn(p=disk_path):
                Image.open(p).load()

            stats_mem = _bench(mem_path, iterations=iters)
            stats_disk = _bench(disk_path_fn, iterations=iters)

            speedup = stats_disk["mean_ms"] / max(stats_mem["mean_ms"], 0.001)
            print(f"    {label:>12s}  {size_kb:7.1f} KB  "
                  f"{stats_mem['mean_ms']:10.3f} ms  "
                  f"{stats_disk['mean_ms']:10.3f} ms  "
                  f"{speedup:6.2f}x")
