"""Tests for WatchdogHandler's ignore window.

The core scenario: exiftool -overwrite_original produces multiple filesystem
events (delete + create/rename) for the same path.  The watchdog handler must
suppress ALL of them after ignore_next_modification() is called, otherwise the
transient delete event triggers db_cleanup_deleted which cascade-deletes
image_tags via the FK constraint.
"""

import time
from unittest.mock import MagicMock

import pytest

from filewatcher.watcher import WatchdogHandler, _IGNORE_WINDOW_SECS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: str, src_path: str, is_directory: bool = False):
    """Create a minimal mock filesystem event."""
    ev = MagicMock()
    ev.event_type = event_type
    ev.src_path = src_path
    ev.dest_path = src_path
    ev.is_directory = is_directory
    return ev


def _make_handler():
    """Create a WatchdogHandler with a mocked ThumbnailManager."""
    tm = MagicMock()
    tm.render_manager = MagicMock()
    tm.metadata_db = MagicMock()
    handler = WatchdogHandler(tm, [], is_daemon_mode=True)
    return handler, tm


def _make_handler_with_db(db):
    """Create a WatchdogHandler whose submit_task executes db_cleanup_deleted
    synchronously — simulating what the render manager does in the real daemon."""
    tm = MagicMock()
    tm.metadata_db = db
    tm.create_tasks_for_file.return_value = []

    def _submit_task(task_id, priority, func, *args, **kwargs):
        func(*args)

    tm.render_manager.submit_task.side_effect = _submit_task
    handler = WatchdogHandler(tm, [], is_daemon_mode=True)
    return handler


# ---------------------------------------------------------------------------
# Tests — ignore window mechanism
# ---------------------------------------------------------------------------

class TestIgnoreWindow:
    """Tests for the time-windowed ignore mechanism."""

    def test_single_event_suppressed(self):
        handler, tm = _make_handler()
        path = "/tmp/test/image.jpg"

        handler.ignore_next_modification(path)
        handler.dispatch(_make_event("deleted", path))

        tm.render_manager.submit_task.assert_not_called()

    def test_multiple_events_suppressed_within_window(self):
        """Exiftool produces delete + created; both must be suppressed."""
        handler, tm = _make_handler()
        path = "/tmp/test/image.jpg"

        handler.ignore_next_modification(path)

        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))
        handler.dispatch(_make_event("modified", path))

        tm.render_manager.submit_task.assert_not_called()
        tm.create_tasks_for_file.assert_not_called()

    def test_events_processed_after_window_expires(self):
        """After the window, events should be processed normally."""
        handler, tm = _make_handler()
        path = "/tmp/test/image.jpg"
        tm.create_tasks_for_file.return_value = []

        handler.ignore_next_modification(path)
        # Force the deadline into the past.
        handler._ignore_until[path] = time.monotonic() - 1.0

        handler.dispatch(_make_event("modified", path))

        tm.create_tasks_for_file.assert_called_once_with(path, pytest.importorskip("core.rendermanager").Priority.LOW)

    def test_delete_after_window_triggers_cleanup(self):
        """A real delete after the window must trigger db_cleanup_deleted."""
        handler, tm = _make_handler()
        path = "/tmp/test/image.jpg"

        handler.ignore_next_modification(path)
        handler._ignore_until[path] = time.monotonic() - 1.0

        handler.dispatch(_make_event("deleted", path))

        tm.render_manager.submit_task.assert_called_once()
        call_args = tm.render_manager.submit_task.call_args
        assert "db_cleanup_deleted" in call_args[0][0]

    def test_unrelated_path_not_affected(self):
        """Ignoring path A must not suppress events for path B."""
        handler, tm = _make_handler()
        path_a = "/tmp/test/a.jpg"
        path_b = "/tmp/test/b.jpg"
        tm.create_tasks_for_file.return_value = []

        handler.ignore_next_modification(path_a)

        handler.dispatch(_make_event("modified", path_b))
        tm.create_tasks_for_file.assert_called_once()

    def test_exiftool_tmp_always_ignored(self):
        handler, tm = _make_handler()
        handler.dispatch(_make_event("created", "/tmp/test/image.jpg_exiftool_tmp"))
        tm.render_manager.submit_task.assert_not_called()

    def test_directory_events_ignored(self):
        handler, tm = _make_handler()
        handler.dispatch(_make_event("created", "/tmp/test/subdir", is_directory=True))
        tm.render_manager.submit_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — end-to-end: tag/rating survives exiftool atomic replace
# ---------------------------------------------------------------------------

class TestExiftoolAtomicReplace:
    """Integration tests that exercise the real database to prove tags and
    ratings survive the exiftool atomic-replace event sequence."""

    def test_tag_survives_atomic_replace(self, tmp_env, sample_images):
        """Tags must not be cascade-deleted by the transient FileDeletedEvent."""
        db = tmp_env["db"]
        path = sample_images[0]

        db.batch_ensure_records_exist([path])
        db.batch_set_tags([path], ["animal"])
        assert db.get_image_tags(path) == ["animal"]

        handler = _make_handler_with_db(db)
        handler.ignore_next_modification(path)

        # Simulate exiftool's atomic replace event sequence.
        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))
        handler.dispatch(_make_event("modified", path))

        # Tags must still be in the DB.
        assert db.get_image_tags(path) == ["animal"]

    def test_rating_survives_atomic_replace(self, tmp_env, sample_images):
        """Ratings live in image_metadata; the row must not be deleted."""
        db = tmp_env["db"]
        path = sample_images[0]

        db.batch_ensure_records_exist([path])
        db.set_rating(path, 4)
        assert db.get_rating(path) == 4

        handler = _make_handler_with_db(db)
        handler.ignore_next_modification(path)

        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))

        assert db.get_rating(path) == 4

    def test_tagged_image_appears_in_filter_after_write(self, tmp_env, sample_images):
        """After tagging + exiftool write-back, the filter must still find the image."""
        db = tmp_env["db"]
        paths = sample_images[:5]

        # Seed all images in the DB.
        db.batch_ensure_records_exist(paths)

        # Tag only the first image.
        db.batch_set_tags([paths[0]], ["animal"])

        # Simulate exiftool atomic replace for the tagged image.
        handler = _make_handler_with_db(db)
        handler.ignore_next_modification(paths[0])
        handler.dispatch(_make_event("deleted", paths[0]))
        handler.dispatch(_make_event("created", paths[0]))

        # Filter by "animal" tag — must return the tagged image.
        star_filter = [True, True, True, True, True, True]
        filtered = db.get_filtered_file_paths("", star_filter, tag_names=["animal"])
        assert paths[0] in filtered

    def test_write_tags_propagates_ignore_to_watcher(self, tmp_env, sample_images):
        """write_tags_to_file must call ignore_next_modification on the watcher.

        This is the exact wiring that failed in production: ThumbnailManager was
        created without a watchdog_handler reference, so the ignore call was
        silently skipped and the exiftool atomic-replace events cascade-deleted
        the tags.
        """
        from core.thumbnail_manager import ThumbnailManager
        db = tmp_env["db"]
        path = sample_images[0]

        db.batch_ensure_records_exist([path])
        db.batch_set_tags([path], ["animal"])

        config = tmp_env["config"]
        tm = ThumbnailManager(config, db, num_workers=1)

        handler = _make_handler_with_db(db)
        # Wire the back-reference the way the daemon must.
        tm.watchdog_handler = handler

        # Stub the plugin so write_tags_to_file reaches the ignore call
        # but doesn't need a real exiftool binary.
        mock_plugin = MagicMock()
        mock_plugin.is_available.return_value = True
        mock_plugin.write_tags.return_value = True
        tm.plugin_registry = MagicMock()
        tm.plugin_registry.get_plugin_for_format.return_value = mock_plugin

        tm.write_tags_to_file(path, ["animal"])

        # The watcher must now suppress the exiftool atomic-replace events.
        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))

        assert db.get_image_tags(path) == ["animal"]
        tm.shutdown()

    def test_write_rating_propagates_ignore_to_watcher(self, tmp_env, sample_images):
        """write_rating_to_file must call ignore_next_modification on the watcher."""
        from core.thumbnail_manager import ThumbnailManager
        db = tmp_env["db"]
        path = sample_images[0]

        db.batch_ensure_records_exist([path])
        db.set_rating(path, 5)

        config = tmp_env["config"]
        tm = ThumbnailManager(config, db, num_workers=1)

        handler = _make_handler_with_db(db)
        tm.watchdog_handler = handler

        mock_plugin = MagicMock()
        mock_plugin.is_available.return_value = True
        mock_plugin.write_rating.return_value = True
        tm.plugin_registry = MagicMock()
        tm.plugin_registry.get_plugin_for_format.return_value = mock_plugin

        tm.write_rating_to_file(path, 5)

        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))

        assert db.get_rating(path) == 5
        tm.shutdown()

    def test_without_ignore_tags_are_lost(self, tmp_env, sample_images):
        """Proves the bug: without ignore_next_modification, the delete event
        wipes the image_metadata row and cascades to image_tags."""
        db = tmp_env["db"]
        path = sample_images[0]

        db.batch_ensure_records_exist([path])
        db.batch_set_tags([path], ["animal"])
        assert db.get_image_tags(path) == ["animal"]

        handler = _make_handler_with_db(db)
        # Deliberately do NOT call ignore_next_modification.

        handler.dispatch(_make_event("deleted", path))
        handler.dispatch(_make_event("created", path))

        # The delete event wiped the image_metadata row (CASCADE → image_tags).
        assert db.get_image_tags(path) == []

        # Filter must NOT find it anymore.
        star_filter = [True, True, True, True, True, True]
        filtered = db.get_filtered_file_paths("", star_filter, tag_names=["animal"])
        assert path not in filtered
