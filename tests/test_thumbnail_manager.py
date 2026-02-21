import os
import sys
import time
import shutil
import pytest
from PIL import Image

# Add project root to Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from core.thumbnail_manager import ThumbnailManager
from core.metadata_database import MetadataDatabase
from core.rendermanager import Priority
from tests.conftest import MockConfigManager

# Test constants
TEST_DIR = os.path.join(os.path.dirname(__file__), 'test_data')
TEST_CACHE_DIR = os.path.join(TEST_DIR, 'cache')
TEST_DB_PATH = os.path.join(TEST_DIR, 'test_thumbnails.db')
TEST_IMAGE = os.path.join(TEST_DIR, 'test_image.jpg')
TEST_IMAGE_COPY = os.path.join(TEST_DIR, 'test_image_copy.jpg')

THUMBNAIL_SIZE = 128

@pytest.fixture(scope="function")
def clean_environment():
    """Set up and tear down test environment."""
    os.makedirs(TEST_DIR, exist_ok=True)
    os.makedirs(TEST_CACHE_DIR, exist_ok=True)

    img = Image.new('RGB', (800, 600), color='red')
    img.save(TEST_IMAGE)
    shutil.copy2(TEST_IMAGE, TEST_IMAGE_COPY)

    yield

    shutil.rmtree(TEST_DIR)

def test_thumbnail_caching_with_modifications(clean_environment):
    """Tests if thumbnails are correctly generated, cached, and updates detected."""
    config = MockConfigManager({"cache_dir": TEST_CACHE_DIR, "thumbnail_size": THUMBNAIL_SIZE})
    metadata_db = MetadataDatabase(TEST_DB_PATH)
    thumbnail_manager = ThumbnailManager(config, metadata_db, num_workers=2)
    thumbnail_manager.load_plugins()

    # Generate initial thumbnail
    original_thumbnail_path = thumbnail_manager.get_thumbnail(TEST_IMAGE_COPY)
    assert original_thumbnail_path is not None
    assert os.path.exists(original_thumbnail_path)

    # Verify thumbnail dimensions match the configured size
    with Image.open(original_thumbnail_path) as thumb:
        assert max(thumb.size) == THUMBNAIL_SIZE

    # Get thumbnail again - should use cache
    cached_thumbnail_path = thumbnail_manager.get_thumbnail(TEST_IMAGE_COPY)
    assert cached_thumbnail_path == original_thumbnail_path

    # Modify test image
    time.sleep(0.1)  # Ensure mtime differs
    img = Image.new('RGB', (800, 600), color='blue')
    img.save(TEST_IMAGE_COPY)

    # Get thumbnail after modification - should generate a new one
    new_thumbnail_path = thumbnail_manager.get_thumbnail(TEST_IMAGE_COPY)
    assert new_thumbnail_path is not None
    assert os.path.exists(new_thumbnail_path)

    # Test async thumbnail generation
    img = Image.new('RGB', (800, 600), color='green')
    img.save(TEST_IMAGE)

    assert thumbnail_manager.request_thumbnail(TEST_IMAGE, Priority.GUI_REQUEST)

    # Give workers time to process
    time.sleep(0.5)

    async_thumbnail_path = thumbnail_manager.get_thumbnail(TEST_IMAGE)
    assert async_thumbnail_path is not None
    assert os.path.exists(async_thumbnail_path)

    thumbnail_manager.shutdown()
