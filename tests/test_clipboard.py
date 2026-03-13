"""Tests for gui.clipboard — copy paths, files, and image pixels."""

import sys
import types
from unittest.mock import MagicMock, patch
import pytest

# Ensure gui.clipboard can be imported with stubs — QMimeData and QUrl need
# to exist on the stub modules before the module is loaded.
_qtcore = sys.modules['PySide6.QtCore']
_qtwidgets = sys.modules['PySide6.QtWidgets']

if not hasattr(_qtcore, 'QUrl'):
    class _QUrl:
        def __init__(self, s=""): self._s = s
        @staticmethod
        def fromLocalFile(p):
            u = _QUrl()
            u._s = p
            return u
        def toString(self): return self._s
    _qtcore.QUrl = _QUrl

if not hasattr(_qtcore, 'QMimeData'):
    class _QMimeData:
        def __init__(self):
            self._urls = []
            self._data = {}
        def setUrls(self, urls): self._urls = urls
        def urls(self): return self._urls
        def setData(self, mime, data): self._data[mime] = data
    _qtcore.QMimeData = _QMimeData

# Now import the module under test
import gui.clipboard as clipboard_mod


@pytest.fixture()
def mock_clipboard():
    """Patch QApplication.clipboard() to return a MagicMock."""
    cb = MagicMock()
    with patch.object(clipboard_mod, "QApplication") as mock_app:
        mock_app.clipboard.return_value = cb
        yield cb


class TestCopyPathsAsText:
    def test_empty_list_returns_zero(self, mock_clipboard):
        assert clipboard_mod.copy_paths_as_text([]) == 0
        mock_clipboard.setText.assert_not_called()

    def test_single_path(self, mock_clipboard):
        assert clipboard_mod.copy_paths_as_text(["/a/b.jpg"]) == 1
        mock_clipboard.setText.assert_called_once_with("/a/b.jpg")

    def test_multiple_paths_newline_separated(self, mock_clipboard):
        paths = ["/a/b.jpg", "/c/d.png", "/e/f.cr3"]
        assert clipboard_mod.copy_paths_as_text(paths) == 3
        mock_clipboard.setText.assert_called_once_with("/a/b.jpg\n/c/d.png\n/e/f.cr3")


class TestCopyFilesToClipboard:
    def test_empty_list_returns_zero(self, mock_clipboard):
        assert clipboard_mod.copy_files_to_clipboard([]) == 0
        mock_clipboard.setMimeData.assert_not_called()

    def test_sets_mime_data_with_urls(self, mock_clipboard):
        paths = ["/a/b.jpg", "/c/d.png"]
        assert clipboard_mod.copy_files_to_clipboard(paths) == 2
        mock_clipboard.setMimeData.assert_called_once()
        mime = mock_clipboard.setMimeData.call_args[0][0]
        urls = mime.urls()
        assert len(urls) == 2


class TestCopyImagePixels:
    def test_none_path_returns_false(self, mock_clipboard):
        assert clipboard_mod.copy_image_pixels(MagicMock(), None) is False

    def test_non_jpeg_returns_false(self, mock_clipboard):
        assert clipboard_mod.copy_image_pixels(MagicMock(), "/a/b.png") is False
        assert clipboard_mod.copy_image_pixels(MagicMock(), "/a/b.cr3") is False

    def test_null_image_returns_false(self, mock_clipboard):
        image = MagicMock()
        image.isNull.return_value = True
        assert clipboard_mod.copy_image_pixels(image, "/a/b.jpg") is False

    def test_none_image_returns_false(self, mock_clipboard):
        assert clipboard_mod.copy_image_pixels(None, "/a/b.jpg") is False

    def test_valid_jpeg_copies_image(self, mock_clipboard):
        image = MagicMock()
        image.isNull.return_value = False
        assert clipboard_mod.copy_image_pixels(image, "/a/b.jpg") is True
        mock_clipboard.setImage.assert_called_once_with(image)

    def test_jpeg_extension_case_insensitive(self, mock_clipboard):
        image = MagicMock()
        image.isNull.return_value = False
        assert clipboard_mod.copy_image_pixels(image, "/a/b.JPG") is True
        assert clipboard_mod.copy_image_pixels(image, "/a/b.JPEG") is True
