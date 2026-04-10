"""Dialog for assigning an OCIO color space to selected files or a folder."""

import logging
import os
from typing import List, Optional

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLineEdit, QComboBox, QPushButton,
    QRadioButton, QVBoxLayout, QButtonGroup, QWidget,
)

logger = logging.getLogger(__name__)


class OcioDialog(QDialog):
    """Modal dialog for assigning OCIO color transforms to selected paths.

    Parameters
    ----------
    paths:
        Selected file/folder paths to assign OCIO to.
    service:
        ThumbnailService instance used to persist the assignment.
    config_path:
        Pre-filled OCIO config path (from global config default).
    """

    def __init__(
        self,
        paths: List[str],
        service,
        config_path: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assign OCIO Color Space")
        self.setMinimumWidth(480)
        self._paths = paths
        self._service = service

        layout = QVBoxLayout(self)

        # -- Scope selection (file vs folder) --------------------------------
        scope_widget = QWidget()
        scope_row = QHBoxLayout(scope_widget)
        scope_row.setContentsMargins(0, 0, 0, 0)
        self._scope_group = QButtonGroup(self)
        self._radio_file = QRadioButton("Apply to selected files only")
        self._radio_folder = QRadioButton("Apply to folder (affects subfolders)")
        self._scope_group.addButton(self._radio_file)
        self._scope_group.addButton(self._radio_folder)
        self._radio_file.setChecked(True)
        scope_row.addWidget(self._radio_file)
        scope_row.addWidget(self._radio_folder)
        layout.addWidget(scope_widget)

        # -- Form ------------------------------------------------------------
        form = QFormLayout()

        # OCIO config path
        config_row = QHBoxLayout()
        self._config_edit = QLineEdit(config_path)
        self._config_edit.setPlaceholderText("Path to config.ocio …")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_config)
        config_row.addWidget(self._config_edit)
        config_row.addWidget(browse_btn)
        form.addRow("OCIO config:", config_row)

        # Input color space
        self._space_combo = QComboBox()
        self._space_combo.setEditable(True)
        self._space_combo.setPlaceholderText("e.g. sRGB, ACEScg …")
        form.addRow("Input color space:", self._space_combo)

        # Display (optional)
        self._display_combo = QComboBox()
        self._display_combo.setEditable(True)
        self._display_combo.addItem("")  # empty = use config default
        self._display_combo.setPlaceholderText("Display (optional)")
        form.addRow("Display:", self._display_combo)

        # View (optional)
        self._view_combo = QComboBox()
        self._view_combo.setEditable(True)
        self._view_combo.addItem("")  # empty = use config default
        self._view_combo.setPlaceholderText("View (optional)")
        form.addRow("View:", self._view_combo)

        layout.addLayout(form)

        # Populate combos if a config path is already filled in
        if config_path:
            self._populate_from_config(config_path)

        self._config_edit.editingFinished.connect(
            lambda: self._populate_from_config(self._config_edit.text())
        )

        # -- Buttons ---------------------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select OCIO Config",
            os.path.expanduser("~"),
            "OCIO Config (*.ocio);;All Files (*)",
        )
        if path:
            self._config_edit.setText(path)
            self._populate_from_config(path)

    def _populate_from_config(self, config_path: str) -> None:
        """Try to load the OCIO config and populate combo boxes."""
        config_path = config_path.strip()
        if not config_path or not os.path.isfile(config_path):  # disk-io: validate config path before parsing
            return
        try:
            import PyOpenColorIO as ocio  # type: ignore[import-untyped]
            cfg = ocio.Config.CreateFromFile(config_path)

            # Color spaces
            self._space_combo.clear()
            for cs in cfg.getColorSpaces():
                self._space_combo.addItem(cs.getName())

            # Displays
            self._display_combo.clear()
            self._display_combo.addItem("")
            for disp in cfg.getDisplays():
                self._display_combo.addItem(disp)

            # Views for the default display
            default_disp = cfg.getDefaultDisplay()
            self._view_combo.clear()
            self._view_combo.addItem("")
            for v in cfg.getViews(default_disp):
                self._view_combo.addItem(v)

        except ImportError:
            logger.warning("PyOpenColorIO not installed — combo boxes not populated")
        except Exception as e:
            logger.warning("Failed to parse OCIO config %s: %s", config_path, e)

    def _on_accept(self) -> None:
        config_path = self._config_edit.text().strip()
        input_space = self._space_combo.currentText().strip()
        display = self._display_combo.currentText().strip()
        view = self._view_combo.currentText().strip()

        if not config_path or not input_space:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Missing fields", "Config path and input color space are required.")
            return

        is_folder = self._radio_folder.isChecked()

        if is_folder:
            # For folder scope, assign to the parent folder of each selected path
            folders = {
                p if os.path.isdir(p) else os.path.dirname(p)  # disk-io: distinguish file vs folder paths
                for p in self._paths
            }
            for folder in folders:
                self._service.set_ocio_assignment(
                    folder, True, config_path, input_space, display, view
                )
            logger.info("OCIO assigned to %d folder(s)", len(folders))
        else:
            for path in self._paths:
                if not os.path.isdir(path):  # disk-io: skip folder cards in selection
                    self._service.set_ocio_assignment(
                        path, False, config_path, input_space, display, view
                    )
            logger.info("OCIO assigned to %d file(s)", len(self._paths))

        self.accept()
