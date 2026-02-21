# config/hotkeys.py

from dataclasses import dataclass
from typing import List

@dataclass
class HotkeyDefinition:
    """Represents a single hotkey entry with its action name, sequences, etc."""
    action_name: str
    sequences: List[str]  # Support multiple key sequences
    description: str
    
    @classmethod
    def from_config(cls, action_name: str, config) -> 'HotkeyDefinition':
        """Create a HotkeyDefinition from any config format"""
        sequences = []
        description = ""
        
        # Handle string config (single sequence)
        if isinstance(config, str):
            sequences = [config]
        # Handle dict config (with optional multiple sequences)
        elif isinstance(config, dict):
            if "sequence" in config:
                sequences.append(config["sequence"])
            if "extra_sequences" in config:
                sequences.extend(config["extra_sequences"])
            description = config.get("description", "")
            
        return cls(
            action_name=action_name,
            sequences=sequences,
            description=description
        )
