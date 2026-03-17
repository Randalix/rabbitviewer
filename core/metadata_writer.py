import logging
import os

from core.priority import Priority, TaskType
from plugins.base_plugin import sidecar_path_for

logger = logging.getLogger(__name__)


class MetadataWriter:
    """File write-back dispatcher for rating, tags, and orientation.

    Routes writes to the correct plugin method (embedded vs sidecar) based on
    config. Owns the ``_WRITE_DISPATCH`` table and pending-write recovery.
    """

    # Maps write_type → plugin method names and ledger payload key.
    _WRITE_DISPATCH = {
        'rating': {
            'embedded': 'write_rating_embedded',
            'sidecar': 'write_rating',
            'payload_key': 'rating',
        },
        'tags': {
            'embedded': 'write_tags_embedded',
            'sidecar': 'write_tags',
            'payload_key': 'tags',
        },
        'orientation': {
            'embedded': 'write_orientation_embedded',
            'sidecar': 'write_orientation',
            'payload_key': 'orientation',
        },
    }

    def __init__(self, config_manager, plugin_registry, metadata_db, render_manager,
                 watchdog_handler=None):
        self.config_manager = config_manager
        self.plugin_registry = plugin_registry
        self.metadata_db = metadata_db
        self.render_manager = render_manager
        self.watchdog_handler = watchdog_handler

    def _resolve_write_mode(self, ext: str) -> str:
        overrides = self.config_manager.get("metadata.format_write_mode", {})
        if ext in overrides:
            return overrides[ext]
        return self.config_manager.get("metadata.default_write_mode", "sidecar")

    def _write_to_file(self, file_path: str, write_type: str, value) -> bool:
        dispatch = self._WRITE_DISPATCH[write_type]

        # why: callers (ThumbnailService, recover_pending_writes) run in
        # RenderManager worker threads that have already passed the volume
        # accessibility check — this guard only catches files deleted between
        # the DB write and the async EXIF write.
        if not os.path.exists(file_path):  # disk-io: write guard
            logger.warning("File not found, cannot write %s: %s", write_type, file_path)
            return False

        ext = os.path.splitext(file_path)[1].lower()
        mode = self._resolve_write_mode(ext)

        if self.watchdog_handler:
            suppress_path = file_path if mode == "embedded" else sidecar_path_for(file_path)
            self.watchdog_handler.ignore_next_modification(suppress_path)

        plugin = self.plugin_registry.get_plugin_for_format(ext)
        if plugin and plugin.is_available():
            success = getattr(plugin, dispatch[mode])(file_path, value)
            if success:
                self.metadata_db.ledgers.pending_write_remove(
                    file_path, write_type, {dispatch['payload_key']: value})
            else:
                logger.error("Plugin failed to write %s for %s", write_type, file_path)
            return success

        logger.warning("No plugin found for format %s to write %s for %s", ext, write_type, file_path)
        return False

    def write_rating(self, file_path: str, rating: int) -> bool:
        return self._write_to_file(file_path, 'rating', rating)

    def write_tags(self, file_path: str, tag_names: list) -> bool:
        return self._write_to_file(file_path, 'tags', tag_names)

    def write_orientation(self, file_path: str, orientation: int) -> bool:
        return self._write_to_file(file_path, 'orientation', orientation)

    def recover_pending_writes(self) -> int:
        pending = self.metadata_db.ledgers.pending_write_get_all()
        if not pending:
            return 0

        count = 0
        for row in pending:
            fp = row['file_path']
            wt = row['write_type']
            dispatch = self._WRITE_DISPATCH.get(wt)
            if not dispatch:
                logger.warning("Unknown pending write type: %s for %s", wt, fp)
                continue
            value = row['payload'][dispatch['payload_key']]
            self.render_manager.submit_task(
                f"write_{wt}::{fp}", Priority.NORMAL,
                self._write_to_file, fp, wt, value,
                task_type=TaskType.SIMPLE,
            )
            count += 1

        logger.info("Recovered %d pending file writes from prior session", count)
        return count
