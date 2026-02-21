from PySide6.QtCore import QObject, QPointF
from core.event_system import event_system, EventType, InspectorEventData
import time
import logging


class InspectorEventHandler(QObject):
    def __init__(self, source_name: str):
        super().__init__()
        self.source_name = source_name
        
    def publish_inspector_event(self, image_path: str, widget_pos: QPointF, widget_size):
        try:
            if widget_size.width() > 0 and widget_size.height() > 0:
                norm_x = max(0.0, min(1.0, widget_pos.x() / widget_size.width()))
                # Invert Y coordinate for consistent coordinate system
                norm_y = max(0.0, min(1.0, 1.0 - (widget_pos.y() / widget_size.height())))
                
                norm_pos = QPointF(norm_x, norm_y)
                
                event_data = InspectorEventData(
                    event_type=EventType.INSPECTOR_UPDATE,
                    source=self.source_name,
                    timestamp=time.time(),
                    image_path=image_path,
                    normalized_position=norm_pos
                )
                event_system.publish(event_data)
                logging.debug(f"Published inspector event from {self.source_name}: {image_path} at {norm_x:.2f}, {norm_y:.2f}")
                
        except Exception as e:
            # why: guard against bad widget_size or event_system errors without crashing the caller
            logging.error(f"Error publishing inspector event from {self.source_name}: {e}", exc_info=True)
