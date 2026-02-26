import os
import logging
import struct
import threading
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Set, Any, Union
import importlib.util
import sys
from PIL import Image
from plugins.exiftool_process import ExifToolProcess

def sidecar_path_for(image_path: str) -> str:
    """Return the XMP sidecar path for an image: /dir/photo.cr3 -> /dir/photo.cr3.xmp"""
    return image_path + ".xmp"


def find_image_for_sidecar(xmp_path: str, supported_extensions: set) -> Optional[str]:
    """Given /dir/photo.jpg.xmp, find the corresponding image file.

    Double-extension convention: strip the trailing ``.xmp`` to recover
    the original image path and verify it exists with a supported extension.
    """
    if not xmp_path.lower().endswith(".xmp"):
        return None
    candidate = xmp_path[:-4]  # strip ".xmp"
    _, ext = os.path.splitext(candidate)
    if ext.lower() in supported_extensions and os.path.exists(candidate):
        return candidate
    return None


class PluginRegistry:
    """Central registry for all image format plugins."""
    
    def __init__(self):
        self.plugins: Dict[str, 'BasePlugin'] = {}
        self.format_map: Dict[str, 'BasePlugin'] = {}
        
    def register_plugin(self, plugin: 'BasePlugin'):
        """Register a plugin and its supported formats."""
        plugin_name = plugin.__class__.__name__
        if plugin_name in self.plugins:
            # Update mutable settings (thumbnail_size, cache_dir) on the existing instance
            # rather than skipping, so config changes take effect without a full reload.
            existing = self.plugins[plugin_name]
            existing.thumbnail_size = plugin.thumbnail_size
            existing.cache_dir = plugin.cache_dir
            existing.thumbnail_cache_dir = plugin.thumbnail_cache_dir
            existing.image_cache_dir = plugin.image_cache_dir
            return

        self.plugins[plugin_name] = plugin
        
        formats = plugin.get_supported_formats()
        for ext in formats:
            if ext in self.format_map:
                logging.warning(f"Format {ext} already registered by {self.format_map[ext].__class__.__name__}, overriding with {plugin_name}")
            self.format_map[ext] = plugin
            logging.debug(f"Registered format {ext} with plugin {plugin_name}")
        
        logging.info(f"Plugin {plugin_name} registered with formats: {', '.join(formats)}")
    
    def get_plugin_for_format(self, file_extension: str) -> Optional['BasePlugin']:
        """Get the plugin that handles a specific file format."""
        # Ensure the extension starts with a dot and is lowercase
        if not file_extension.startswith('.'):
            file_extension = '.' + file_extension
        return self.format_map.get(file_extension.lower())
    
    def get_supported_formats(self) -> Set[str]:
        """Get all supported file formats across all plugins."""
        return set(self.format_map.keys())

    def load_plugins_from_directory(self, plugin_dir: str, cache_dir: str, thumbnail_size: int = 64):
        """
        Loads all plugins from a given directory and registers them.
        """
        logging.info(f"Loading plugins from directory: {plugin_dir}")
        # Add plugin directory to sys.path to allow direct imports
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

        for filename in os.listdir(plugin_dir):
            if filename.endswith('_plugin.py') and filename != 'base_plugin.py':
                module_name = filename[:-3] # Remove .py
                try:
                    # Create a module spec from the file path.
                    # Use the fully-qualified package name so that relative imports
                    # (e.g. `from .base_plugin import BasePlugin`) resolve correctly.
                    file_path = os.path.join(plugin_dir, filename)
                    full_module_name = f"plugins.{module_name}"
                    spec = importlib.util.spec_from_file_location(full_module_name, file_path)
                    if spec is None:
                        logging.warning(f"Could not create module spec for {filename}")
                        continue

                    module = importlib.util.module_from_spec(spec)
                    sys.modules[full_module_name] = module
                    spec.loader.exec_module(module)
                    
                    # Iterate through the module's attributes to find BasePlugin subclasses
                    for attribute_name in dir(module):
                        attribute = getattr(module, attribute_name)
                        if isinstance(attribute, type) and issubclass(attribute, BasePlugin) and attribute is not BasePlugin:
                            # Instantiate the plugin and it will self-register if available
                            plugin_instance = attribute(cache_dir=cache_dir, thumbnail_size=thumbnail_size)
                            # The BasePlugin constructor now handles logging and registration.
                            break # Assume one plugin class per file
                except Exception as e:
                    logging.error(f"Failed to load plugin {filename}: {e}")
                    logging.exception(f"Detailed error loading plugin {filename}:")
        logging.info("Finished loading plugins.")


# Global plugin registry instance
plugin_registry = PluginRegistry()

class BasePlugin(ABC):
    """Base class for all image format plugins."""
    
    def __init__(self, cache_dir: str, thumbnail_size: int = 64):
        self.cache_dir = cache_dir
        self.thumbnail_size = thumbnail_size
        self.thumbnail_cache_dir = os.path.join(cache_dir, "thumbnails")
        self.image_cache_dir = os.path.join(cache_dir, "images")
        
        # Ensure cache directories exist
        os.makedirs(self.thumbnail_cache_dir, exist_ok=True)
        os.makedirs(self.image_cache_dir, exist_ok=True)
        
        # Check availability and register if available
        if self.is_available():
            self.register_formats()
            logging.info(f"Plugin {self.__class__.__name__} loaded successfully")
        else:
            logging.warning(f"Plugin {self.__class__.__name__} not available - missing dependencies")
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if all required dependencies for this plugin are available."""
        pass
    
    @abstractmethod
    def get_supported_formats(self) -> List[str]:
        """Return list of supported file extensions (with dots, lowercase)."""
        pass
    
    def register_formats(self):
        """Register this plugin for its supported formats."""
        plugin_registry.register_plugin(self)
    
    @abstractmethod
    def generate_view_image(self, image_path: str, image_source: Union[str, bytes], orientation: int, output_path: str) -> bool:
        """
        Convert image to JPG for viewing and save to output_path.
        image_source is either a file path (str) or in-memory image bytes (bytes).
        Returns True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def generate_thumbnail(self, image_path: str, image_source: Optional[Union[str, bytes]], orientation: int, output_path: str) -> bool:
        """
        Generate thumbnail JPG and save to output_path.
        image_source is either a file path (str), in-memory image bytes (bytes), or None to fall back to image_path.
        Returns True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """
        Process an image to create only its thumbnail. This should be as fast
        as possible, prioritizing embedded thumbnails if available.

        ``prefetch_buffer`` is the first N bytes of the file already read by
        the caller.  Plugins may use it to avoid a second NAS round-trip.
        Returns the path to the generated thumbnail, or None on failure.
        """
        pass

    @abstractmethod
    def process_view_image(self, image_path: str, md5_hash: str) -> Optional[str]:
        """
        Process an image to create its full-resolution view image. This can be slower.
        Returns the path to the generated view image, or None on failure.
        """
        pass

    def extract_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Fast binary scan of the file header for EXIF orientation and XMP rating.
        Reads the first 256 KB; returns None to fall back to the default exiftool
        extractor if the file is missing or the scan yields nothing.
        """
        if not os.path.exists(file_path):
            return None
        results: Dict[str, Any] = {}
        ns = {
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "xmp": "http://ns.adobe.com/xap/1.0/",
        }
        try:
            with open(file_path, "rb") as f:
                buf = f.read(256 * 1024)

            # EXIF Orientation (little-endian IFD tag 0x0112)
            orientation = self._scan_exif_orientation(buf)
            if orientation != 1:
                results["orientation"] = orientation

            # XMP rating from embedded metadata
            start = buf.find(b"<x:xmpmeta")
            if start != -1:
                end = buf.find(b"</x:xmpmeta>", start)
                if end != -1:
                    xmp_str = buf[start: end + len(b"</x:xmpmeta>")].decode("utf-8", "ignore")
                    root = ET.fromstring(xmp_str)
                    desc = root.find(".//rdf:Description", ns)
                    rating_tag = desc.find("xmp:Rating", ns) if desc is not None else None
                    if rating_tag is not None and rating_tag.text:
                        try:
                            results["rating"] = int(rating_tag.text)
                        except (ValueError, TypeError):
                            pass

            # Sidecar override: if FILENAME.xmp exists, its rating takes precedence.
            xmp = sidecar_path_for(file_path)
            if os.path.exists(xmp):
                try:
                    with open(xmp, "rb") as xf:
                        xmp_buf = xf.read(64 * 1024)
                    sc_start = xmp_buf.find(b"<x:xmpmeta")
                    if sc_start != -1:
                        sc_end = xmp_buf.find(b"</x:xmpmeta>", sc_start)
                        if sc_end != -1:
                            sc_str = xmp_buf[sc_start: sc_end + len(b"</x:xmpmeta>")].decode("utf-8", "ignore")
                            sc_root = ET.fromstring(sc_str)
                            sc_desc = sc_root.find(".//rdf:Description", ns)
                            sc_rating = sc_desc.find("xmp:Rating", ns) if sc_desc is not None else None
                            if sc_rating is not None and sc_rating.text:
                                try:
                                    results["rating"] = int(sc_rating.text)
                                except (ValueError, TypeError):
                                    pass
                except (IOError, ET.ParseError) as e:
                    logging.warning("Sidecar parse failed for %s: %s", xmp, e)

            return results if results else None
        except (IOError, struct.error, ET.ParseError) as e:
            logging.warning("Fast metadata parse failed for %s: %s", file_path, e)
            return None

    def get_view_image_path(self, md5_hash: str) -> str:
        """Generates the path for the full resolution view image."""
        return os.path.join(self.image_cache_dir, f"{md5_hash}.jpg")

    def get_thumbnail_path(self, md5_hash: str) -> str:
        """Generates the path for the thumbnail image."""
        return os.path.join(self.thumbnail_cache_dir, f"{md5_hash}.jpg")

    # Thread-local storage for per-thread ExifToolProcess instances.
    _local = threading.local()

    def _get_exiftool(self):
        """Return (or lazily create) the per-thread ExifToolProcess."""
        if not hasattr(self._local, "proc"):
            self._local.proc = ExifToolProcess()
        return self._local.proc

    def write_rating(self, file_path: str, rating: int) -> bool:
        """Writes the rating to an XMP sidecar file next to the image."""
        if not 0 <= rating <= 5:
            logging.error("Rating %d out of range [0..5] for %s", rating, file_path)
            return False
        xmp = sidecar_path_for(file_path)
        try:
            output = self._write_to_sidecar(xmp, [f"-XMP-xmp:Rating={rating}"], file_path)
            if self._sidecar_write_ok(output):
                logging.info("Wrote rating %d to sidecar %s.", rating, xmp)
                return True
            logging.error("exiftool reported no update writing rating sidecar %s: %s",
                          xmp, output.decode("utf-8", "replace").strip())
            return False
        except (RuntimeError, TimeoutError) as e:
            logging.error("Failed to write rating sidecar for %s: %s", file_path, e)
            return False

    def write_tags(self, file_path: str, tag_names: list) -> bool:
        """Writes tags to an XMP sidecar file next to the image.

        Replaces the entire Subject list to keep DB and file in sync.
        For existing sidecars the clear must be a separate exiftool call;
        bag-type XMP tags ignore ``-TAG=`` when ``+=`` appears in the same
        invocation.
        """
        xmp = sidecar_path_for(file_path)
        try:
            et = self._get_exiftool()
            if os.path.exists(xmp):
                # Existing sidecar: clear old tags first, then add new ones.
                et.execute(["-XMP:Subject=", "-overwrite_original", xmp])
                if tag_names:
                    args = [f"-XMP:Subject+={t}" for t in tag_names]
                    output = et.execute(args + ["-overwrite_original", xmp])
                else:
                    output = b"    1 image files updated"
            else:
                # New sidecar: no prior tags to clear.
                args = [f"-XMP:Subject+={t}" for t in tag_names]
                output = et.execute(["-o", xmp] + args + [file_path])
                if b"already exists" in output:
                    et.execute(["-XMP:Subject=", "-overwrite_original", xmp])
                    if tag_names:
                        output = et.execute(
                            [f"-XMP:Subject+={t}" for t in tag_names]
                            + ["-overwrite_original", xmp]
                        )
                    else:
                        output = b"    1 image files updated"
            if self._sidecar_write_ok(output):
                logging.info("Wrote %d tags to sidecar %s.", len(tag_names), xmp)
                return True
            logging.error("exiftool reported no update writing tags sidecar %s: %s",
                          xmp, output.decode("utf-8", "replace").strip())
            return False
        except (RuntimeError, TimeoutError) as e:
            logging.error("Failed to write tags sidecar for %s: %s", file_path, e)
            return False

    def _write_to_sidecar(self, xmp_path: str, tag_args: list, image_path: str) -> bytes:
        """Write *tag_args* to the XMP sidecar at *xmp_path*.

        Creates the sidecar if it doesn't exist; updates in-place otherwise.
        *image_path* is the source image — exiftool needs it to bootstrap a new
        sidecar via ``-o``.  Handles the race where two concurrent writers both
        see "not exists" by retrying with the update path if ``-o`` fails
        because the file appeared.
        """
        et = self._get_exiftool()
        if os.path.exists(xmp_path):
            return et.execute(tag_args + ["-overwrite_original", xmp_path])
        # Create sidecar from the source image's XMP skeleton.
        output = et.execute(["-o", xmp_path] + tag_args + [image_path])
        if b"already exists" in output:
            # Lost the race — another thread created it first; update instead.
            return et.execute(tag_args + ["-overwrite_original", xmp_path])
        return output

    @staticmethod
    def _sidecar_write_ok(output: bytes) -> bool:
        """Return True if exiftool output indicates a successful sidecar write."""
        return (
            (b"image files updated" in output or b"image files created" in output)
            and b"0 image files" not in output
        )

    def _apply_orientation(self, img: Image.Image, orientation: int) -> Image.Image:
        """Apply rotation/flip to a PIL Image based on the EXIF Orientation tag value."""
        T = Image.Transpose
        ops = {
            2: T.FLIP_LEFT_RIGHT,
            3: T.ROTATE_180,
            4: T.FLIP_TOP_BOTTOM,
            5: T.TRANSPOSE,
            6: T.ROTATE_270,
            7: T.TRANSVERSE,
            8: T.ROTATE_90,
        }
        op = ops.get(orientation)
        if op is not None:
            img = img.transpose(op)
        return img

    @staticmethod
    def _scan_exif_orientation(buf: bytes) -> int:
        """
        Fast binary scan for EXIF Orientation tag (little-endian IFD 0x0112).
        Returns the orientation value, or 1 if not found.
        """
        tag_sig = b"\x12\x01\x03\x00\x01\x00\x00\x00"
        pos = buf.find(tag_sig)
        if pos != -1:
            try:
                return struct.unpack("<H", buf[pos + 8: pos + 10])[0]
            except struct.error:
                pass
        return 1
