from PySide6.QtGui import QKeySequence, QShortcut, QKeyEvent
import logging
logger = logging.getLogger(__name__)
import time
from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox
from PySide6.QtCore import Qt, QObject
from config.hotkeys import HotkeyDefinition
from typing import Dict, List, Callable
from core.event_system import event_system, EventType, EventData, RunScriptEventData, OpenModalMenuEventData

_TEXT_INPUT_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox)

# Shortcuts that remain active even when a focus-sink panel has keyboard focus.
_PANEL_PASS_THROUGH = frozenset({
    "navigate_parent",  # Alt+Up — useful from tree context
    "close_or_quit",    # always allow quit
})


class HotkeyManager(QObject):
	def __init__(self, parent_widget, hotkeys_config: dict):
		super().__init__()
		self.setParent(parent_widget)
		self.parent_widget = parent_widget
		self.shortcuts: Dict[str, List[QShortcut]] = {}
		self.definitions: Dict[str, HotkeyDefinition] = {}
		self.actions: Dict[str, Callable] = {}

		self._shortcuts_suppressed = False  # text-input context
		self._in_panel_context = False       # focus-sink panel has focus
		self._focus_sinks: list = []         # registered non-content panels
		self._setup_built_in_action_handlers()
		self.load_config(hotkeys_config)
		app = QApplication.instance()
		if app:
			app.installEventFilter(self)
			app.focusChanged.connect(self._on_focus_changed)
		else:
			logger.warning("HotkeyManager: QApplication instance not found, falling back to parent widget for event filter.")
			parent_widget.installEventFilter(self)
		
	def _setup_built_in_action_handlers(self):
		self.add_action("escape_picture_view", lambda: event_system.publish(EventData(event_type=EventType.ESCAPE_PRESSED, source="hotkey_manager", timestamp=time.time())))
		self.add_action("zoom_in", lambda: None)  # placeholder; main_window overrides
		self.add_action("zoom_out", lambda: None)  # placeholder; main_window overrides
		self.add_action("next_image", lambda: event_system.publish(EventData(event_type=EventType.NAVIGATE_NEXT, source="hotkey_manager", timestamp=time.time())))
		self.add_action("previous_image", lambda: event_system.publish(EventData(event_type=EventType.NAVIGATE_PREVIOUS, source="hotkey_manager", timestamp=time.time())))
		self.add_action("toggle_inspector", lambda: event_system.publish(EventData(event_type=EventType.TOGGLE_INSPECTOR, source="hotkey_manager", timestamp=time.time())))
		self.add_action("open_filter", lambda: event_system.publish(EventData(event_type=EventType.OPEN_FILTER, source="hotkey_manager", timestamp=time.time())))
		self.add_action("open_tag_editor", lambda: event_system.publish(EventData(event_type=EventType.OPEN_TAG_EDITOR, source="hotkey_manager", timestamp=time.time())))
		self.add_action("pin_inspector", lambda: None)  # placeholder; main_window overrides
		self.add_action("show_hotkey_help", lambda: None)  # placeholder; main_window overrides
		self.add_action("toggle_info_panel", lambda: None)  # placeholder; main_window overrides
		self.add_action("navigate_parent", lambda: event_system.publish(EventData(event_type=EventType.NAVIGATE_PARENT, source="hotkey_manager", timestamp=time.time())))
		self.add_action("open_breadcrumb", lambda: event_system.publish(EventData(event_type=EventType.OPEN_BREADCRUMB, source="hotkey_manager", timestamp=time.time())))


	def register_focus_sink(self, widget) -> None:
		"""Register *widget* as a focus sink.

		While *widget* or any descendant has keyboard focus, all shortcuts except
		those in _PANEL_PASS_THROUGH are disabled so the panel can receive keys
		uncontested.  Uses a parent-chain walk rather than isAncestorOf so that
		QAbstractScrollArea viewport children are handled correctly.
		"""
		self._focus_sinks.append(widget)

	def _is_in_focus_sink(self, widget) -> bool:
		"""Return True if *widget* is inside any registered focus sink."""
		w = widget
		while w is not None:
			if w in self._focus_sinks:
				return True
			w = w.parent()
		return False

	def _on_focus_changed(self, old, new):
		is_text = isinstance(new, _TEXT_INPUT_TYPES)
		in_panel = new is not None and self._is_in_focus_sink(new)
		if is_text == self._shortcuts_suppressed and in_panel == self._in_panel_context:
			return
		self._shortcuts_suppressed = is_text
		self._in_panel_context = in_panel
		self._update_shortcut_states()
		logger.debug(
			"HotkeyManager: focus→%s  text=%s  panel=%s",
			type(new).__name__, is_text, in_panel)

	def _update_shortcut_states(self) -> None:
		"""Enable or disable each shortcut based on the current focus context."""
		for action_name, shortcut_list in self.shortcuts.items():
			enabled = (
				not self._shortcuts_suppressed
				and (not self._in_panel_context or action_name in _PANEL_PASS_THROUGH)
			)
			for sc in shortcut_list:
				sc.setEnabled(enabled)

	def eventFilter(self, obj, event):
		if not isinstance(event, QKeyEvent):
			return super().eventFilter(obj, event)

		# Shift and Tab are intercepted manually here; honour text-input suppression
		# for Shift (no shortcuts should fire), but Tab must always reach toggle_tree_panel
		# so it still works to close the panel from panel-context.
		if self._shortcuts_suppressed:
			return super().eventFilter(obj, event)

		# Shift press/release for drag-to-range-selection transition
		if event.key() == Qt.Key_Shift and not event.isAutoRepeat():
			if event.type() == QKeyEvent.Type.KeyPress:
				event_system.publish(EventData(event_type=EventType.SHIFT_PRESSED, source="hotkey_manager", timestamp=time.time()))
			elif event.type() == QKeyEvent.Type.KeyRelease:
				event_system.publish(EventData(event_type=EventType.SHIFT_RELEASED, source="hotkey_manager", timestamp=time.time()))

		# Tab is intercepted here because QShortcut cannot reliably capture it
		# before Qt's focus-traversal machinery consumes it.
		if (event.type() == QKeyEvent.Type.KeyPress
				and event.key() == Qt.Key_Tab
				and not event.isAutoRepeat()):
			handler = self.actions.get("toggle_tree_panel")
			if handler:
				handler()
				return True

		return super().eventFilter(obj, event)
						
	def load_config(self, config: dict):
		for action_name, action_config in config.items():
			try:
				if action_name.startswith("script:"):
					script_name = action_name[7:]
					self.add_action(action_name, lambda s=script_name: (
						event_system.publish(RunScriptEventData(
						event_type=EventType.RUN_SCRIPT, source="hotkey_manager",
						timestamp=time.time(), script_name=s))
					))
				elif action_name.startswith("menu:"):
					menu_id = action_name[5:]
					self.add_action(action_name, lambda m=menu_id: (
						event_system.publish(OpenModalMenuEventData(
						event_type=EventType.OPEN_MODAL_MENU, source="hotkey_manager",
						timestamp=time.time(), menu_id=m))
					))
				
				definition = HotkeyDefinition.from_config(action_name, action_config)
				self.add_hotkey_shortcut(definition)
			except Exception as e:
				# why: skip malformed config entries without aborting the whole load
				logger.error(f"Error loading hotkey config for {action_name}: {e}")

	def add_hotkey_shortcut(self, definition: HotkeyDefinition):
		if not definition.sequences:
			return

		logger.debug(f"Setting up hotkey: {definition.action_name} ({definition.sequences})")

		self.definitions[definition.action_name] = definition
		self.shortcuts[definition.action_name] = []

		for sequence in definition.sequences:
			# Remove any existing shortcut with the same key sequence to avoid
			# Qt "ambiguous shortcut" conflicts (which silently disable both).
			seq_key = QKeySequence(sequence).toString()
			for other_name, other_shortcuts in list(self.shortcuts.items()):
				if other_name == definition.action_name:
					continue
				for sc in list(other_shortcuts):
					if sc.key().toString() == seq_key:
						logger.warning(
							"Hotkey conflict: '%s' takes '%s' from '%s'",
							definition.action_name, seq_key, other_name)
						sc.setEnabled(False)
						sc.deleteLater()
						other_shortcuts.remove(sc)

			shortcut = QShortcut(QKeySequence(sequence), self.parent_widget)
			shortcut.setContext(Qt.ApplicationShortcut)
			shortcut.activated.connect(
				lambda an=definition.action_name: self.on_shortcut_triggered(an)
			)
			self.shortcuts[definition.action_name].append(shortcut)

	def add_action(self, action_name: str, callback: Callable):
		self.actions[action_name] = callback
		logger.debug(f"Registered action '{action_name}' with callback {callback}")

	def on_shortcut_triggered(self, action_name: str):
		logger.debug(f"HotkeyManager.on_shortcut_triggered: '{action_name}' (shortcuts enabled: {any(s.isEnabled() for sl in self.shortcuts.values() for s in sl)})")
		handler = self.actions.get(action_name)
		if handler:
			try:
				handler()
			except Exception as e:
				# why: isolate handler crashes so one broken action can't break other shortcuts
				logger.error(f"Error executing action {action_name}: {e}")
		else:
			logger.error(f"No handler found for action: {action_name}")
