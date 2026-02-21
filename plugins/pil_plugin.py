import logging
from typing import Optional, List, Union
from PIL import Image, ImageOps
from .base_plugin import BasePlugin
from .exiftool_process import is_exiftool_available
import os

class PILPlugin(BasePlugin):
    """Plugin for handling standard image formats using PIL/Pillow."""

    def is_available(self) -> bool:
        """Check if PIL and exiftool are available."""
        return is_exiftool_available()
    
    def get_supported_formats(self) -> List[str]:
        """Return list of supported file extensions."""
        return ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.tif', '.webp']
    
    def generate_view_image(self, image_path: str, image_source: Union[str, bytes], orientation: int, output_path: str) -> bool:
        """
        Convert image to JPG for viewing and save to output_path.
        image_source is the file path to open (for PIL, typically image_path itself).
        Returns True if successful, False otherwise.
        """
        try:
            with Image.open(image_source) as img:
                img = ImageOps.exif_transpose(img)
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')

                # Save as JPEG for viewing
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                img.save(output_path, 'JPEG', quality=95)
                logging.debug(f"Generated view image: {output_path}")
                return True
                
        except (OSError, ValueError) as e:
            logging.error(f"Error generating view image for {image_path} (from {image_source}): {e}")
            return False
    
    def generate_thumbnail(self, image_path: str, image_source: Optional[Union[str, bytes]], orientation: int, output_path: str) -> bool:
        """
        Generate thumbnail JPG and save to output_path.
        image_source is the file path to open, or None to fall back to image_path.
        Returns True if successful, False otherwise.
        """
        source = image_source if image_source else image_path
        try:
            with Image.open(source) as img:
                img = ImageOps.exif_transpose(img)
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')

                # Create thumbnail
                img.thumbnail((self.thumbnail_size, self.thumbnail_size), Image.Resampling.LANCZOS)
                
                # Save thumbnail
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                img.save(output_path, 'JPEG', quality=85)
                logging.debug(f"Generated thumbnail: {output_path}")
                return True
                
        except (OSError, ValueError) as e:
            logging.error(f"Error generating thumbnail for {image_path} (from {source}): {e}")
            return False
    
    def process_thumbnail(self, image_path: str, md5_hash: str,
                          prefetch_buffer: Optional[bytes] = None) -> Optional[str]:
        """Generates a thumbnail directly from the source image.
        prefetch_buffer is accepted for interface compatibility but not used;
        PIL opens the file path directly."""
        thumbnail_path = self.get_thumbnail_path(md5_hash)
        if os.path.exists(thumbnail_path):
            return thumbnail_path
        
        if self.generate_thumbnail(image_path, image_source=image_path, orientation=1, output_path=thumbnail_path):
            return thumbnail_path
        return None

    def process_view_image(self, image_path: str, md5_hash: str) -> Optional[str]:
        """Generates a cached view image (JPG) from the source image."""
        view_image_path = self.get_view_image_path(md5_hash)
        if os.path.exists(view_image_path):
            return view_image_path

        if self.generate_view_image(image_path, image_source=image_path, orientation=1, output_path=view_image_path):
            return view_image_path
        return None

