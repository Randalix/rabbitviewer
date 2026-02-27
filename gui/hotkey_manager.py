from PySide6.QtGui import QKeySequence, QShortcut, QKeyEvent
import logging
import time
from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox
from PySide6.QtCore import Qt, QObject, QKeyCombination
from config.hotkeys import HotkeyDefinition
from typing import Dict, List, Callable
from core.event_system import event_system, EventType, EventData, ZoomEventData

_TEXT_INPUT_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox)


class HotkeyManager(QObject):
	def __init__(self, parent_widget, hotkeys_config: dict):
		super().__init__()
		self.setParent(parent_widget)
		self.parent_widget = parent_widget
		self.shortcuts: Dict[str, List[QShortcut]] = {}
		self.definitions: Dict[str, HotkeyDefinition] = {}
		self.actions: Dict[str, Callable] = {}
		
		self._shortcuts_suppressed = False
		self._setup_built_in_action_handlers()
		self.load_config(hotkeys_config)
		app = QApplication.instance()
		if app:
			app.installEventFilter(self)
			app.focusChanged.connect(self._on_focus_changed)
		else:
			logging.warning("HotkeyManager: QApplication instance not found, falling back to parent widget for event filter.")
			parent_widget.installEventFilter(self)
		
	def _setup_built_in_action_handlers(self):
		self.add_action("escape_picture_view", lambda: event_system.publish(EventData(event_type=EventType.ESCAPE_PRESSED, source="hotkey_manager", timestamp=time.time())))
		self.add_action("zoom_in", self._handle_zoom_in)
		self.add_action("zoom_out", self._handle_zoom_out)
		self.add_action("next_image", lambda: event_system.publish(EventData(event_type=EventType.NAVIGATE_NEXT, source="hotkey_manager", timestamp=time.time())))
		self.add_action("previous_image", lambda: event_system.publish(EventData(event_type=EventType.NAVIGATE_PREVIOUS, source="hotkey_manager", timestamp=time.time())))
		self.add_action("toggle_inspector", lambda: event_system.publish(EventData(event_type=EventType.TOGGLE_INSPECTOR, source="hotkey_manager", timestamp=time.time())))
		self.add_action("open_filter", lambda: event_system.publish(EventData(event_type=EventType.OPEN_FILTER, source="hotkey_manager", timestamp=time.time())))
		self.add_action("open_tag_editor", lambda: event_system.publish(EventData(event_type=EventType.OPEN_TAG_EDITOR, source="hotkey_manager", timestamp=time.time())))
		self.add_action("start_range_selection", self.handle_range_selection_start)
		self.add_action("pin_inspector", lambda: None)  # placeholder; main_window overrides
		self.add_action("show_hotkey_help", lambda: None)  # placeholder; main_window overrides
		self.add_action("toggle_info_panel", lambda: None)  # placeholder; main_window overrides

	def _handle_zoom_in(self):
		logging.debug("Zoom in triggered")
		event_system.publish(ZoomEventData(
			event_type=EventType.ZOOM_IN,
			source="hotkey_manager",
			timestamp=time.time(),
			zoom_factor=1.25,
		))

	def _handle_zoom_out(self):
		logging.debug("Zoom out triggered")
		event_system.publish(ZoomEventData(
			event_type=EventType.ZOOM_OUT,
			source="hotkey_manager",
			timestamp=time.time(),
			zoom_factor=1.25,
		))

	def _on_focus_changed(self, old, new):
		"""Suppress shortcuts while a text-input widget has focus."""
		should_suppress = isinstance(new, _TEXT_INPUT_TYPES)
		if should_suppress == self._shortcuts_suppressed:
			return
		self._shortcuts_suppressed = should_suppress
		for shortcut_list in self.shortcuts.values():
			for shortcut in shortcut_list:
				shortcut.setEnabled(not should_suppress)
		logging.debug(f"HotkeyManager: shortcuts {'suppressed' if should_suppress else 'restored'} (focus → {type(new).__name__})")

	def handle_range_selection_start(self):
		event_system.publish(EventData(event_type=EventType.RANGE_SELECTION_START, source="hotkey_manager", timestamp=time.time()))

	def handle_range_selection_end(self):
		event_system.publish(EventData(event_type=EventType.RANGE_SELECTION_END, source="hotkey_manager", timestamp=time.time()))

	def eventFilter(self, obj, event):
		if not isinstance(event, QKeyEvent):
			return super().eventFilter(obj, event)

		if self._shortcuts_suppressed:
			return super().eventFilter(obj, event)

		range_select_def = self.definitions.get("start_range_selection")
		if not (range_select_def and range_select_def.sequences):
			return super().eventFilter(obj, event)

		key_seq = QKeySequence(range_select_def.sequences[0])
		target_combination = key_seq[0]
		event_combination = QKeyCombination(event.modifiers(), Qt.Key(event.key()))
		if event_combination == target_combination:
			if event.type() == QKeyEvent.Type.KeyPress:
				if not event.isAutoRepeat():
					self.handle_range_selection_start()
				return True
			elif event.type() == QKeyEvent.Type.KeyRelease:
				if not event.isAutoRepeat():
					self.handle_range_selection_end()
				return True
					
		return super().eventFilter(obj, event)
						
	def load_config(self, config: dict):
		for action_name, action_config in config.items():
			try:
				if action_name.startswith("script:"):
					script_name = action_name[7:]
					self.add_action(action_name, lambda s=script_name: (
						self.parent_widget.script_manager.run_script(s)
					))
				elif action_name.startswith("menu:"):
					menu_id = action_name[5:]
					self.add_action(action_name, lambda m=menu_id: (
						self.parent_widget.modal_menu.open(m)
					))
				
				definition = HotkeyDefinition.from_config(action_name, action_config)
				self.add_hotkey_shortcut(definition)
			except Exception as e:
				# why: skip malformed config entries without aborting the whole load
				logging.error(f"Error loading hotkey config for {action_name}: {e}")

	def add_hotkey_shortcut(self, definition: HotkeyDefinition):
		if not definition.sequences:
			return
			
		# This action is handled exclusively by the eventFilter for press/release logic
		if definition.action_name == "start_range_selection":
			logging.debug("Skipping QShortcut for 'start_range_selection', handled by eventFilter.")
			self.definitions[definition.action_name] = definition
			return

		logging.debug(f"Setting up hotkey: {definition.action_name} ({definition.sequences})")
		
		self.definitions[definition.action_name] = definition
		self.shortcuts[definition.action_name] = []
		
		for sequence in definition.sequences:
			shortcut = QShortcut(QKeySequence(sequence), self.parent_widget)
			shortcut.setContext(Qt.ApplicationShortcut)
			shortcut.activated.connect(
				lambda an=definition.action_name: self.on_shortcut_triggered(an)
			)
			self.shortcuts[definition.action_name].append(shortcut)

	def add_action(self, action_name: str, callback: Callable):
		self.actions[action_name] = callback
		logging.debug(f"Registered action '{action_name}' with callback {callback}")

	def on_shortcut_triggered(self, action_name: str):
		logging.debug(f"HotkeyManager.on_shortcut_triggered: '{action_name}' (shortcuts enabled: {any(s.isEnabled() for sl in self.shortcuts.values() for s in sl)})")
		handler = self.actions.get(action_name)
		if handler:
			try:
				handler()
			except Exception as e:
				# why: isolate handler crashes so one broken action can't break other shortcuts
				logging.error(f"Error executing action {action_name}: {e}")
		else:
			logging.error(f"No handler found for action: {action_name}")
