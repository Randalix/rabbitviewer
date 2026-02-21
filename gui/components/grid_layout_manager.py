from PySide6.QtCore import QObject, Signal, QPoint, QSize, QTimer
from PySide6.QtWidgets import QGridLayout, QWidget, QScrollArea
from math import floor, ceil
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from gui.thumbnail_view import ThumbnailLabel


class GridLayoutManager(QObject):
    """Manages grid layout calculations and operations"""
    
    layoutChanged = Signal()
    
    def __init__(self, grid_layout: QGridLayout, grid_container: QWidget, scroll_area: QScrollArea, thumbnail_size: int, spacing: int):
        super().__init__()
        self.grid_layout = grid_layout
        self.grid_container = grid_container
        self.scroll_area = scroll_area
        self.thumbnail_size = thumbnail_size
        self.spacing = spacing
        self._columns = 1
        self._current_files = []
        self._labels: Dict[int, 'ThumbnailLabel'] = {}
        self._initialized = False
        
    def calculate_columns(self, available_width: int) -> int:
        """Calculates the maximum number of columns that fit within the available width."""
        # Account for the constant padding on the left and right of the container.
        content_width = available_width - (2 * self.spacing)
        if content_width < self.thumbnail_size:
            return 1 # Not enough space for one thumbnail, but we always show at least one.
 
        # Calculate how many thumbnails and gaps can fit in the available content area.
        # N*size + (N-1)*spacing <= content_width  =>  N*(size+spacing) <= content_width + spacing
        cell_width = self.thumbnail_size + self.spacing
        if cell_width <= 0:
            return 1
            
        columns = floor((content_width + self.spacing) / cell_width)
        return max(1, columns)
    
    def initialize_layout(self):
        """Initialize layout calculations immediately after setup"""
        if not self._initialized:
            self.update_layout()
            self._initialized = True
    
    def set_files_and_labels(self, files: List[str], labels: Dict[int, 'ThumbnailLabel']):
        """Set the current files and labels and refresh the layout."""
        self._current_files = files
        self._labels = labels
        self.clear_layout()
        self._reposition_widgets()
    
    def get_available_width(self) -> int:
        """Get the current available width from scroll area"""
        available_width = self.scroll_area.viewport().width()
        if available_width <= 0:
            available_width = self.scroll_area.width() if self.scroll_area.width() > 0 else 800
        return available_width
    
    def update_layout(self):
        """
        Recalculates column count based on current width and repositions all widgets if necessary.
        This should be called from the view's resizeEvent.
        """
        available_width = self.get_available_width()
        new_columns = self.calculate_columns(available_width)

        # Optimization: Only reposition if the number of columns has actually changed.
        if new_columns != self._columns:
            self._columns = new_columns
            self._reposition_widgets()
            self.layoutChanged.emit() # Notify view that layout properties (like columns) changed.
    
    def _reposition_widgets(self):
        """Reposition all widgets according to the current layout."""
        # Note: QGridLayout.addWidget() automatically moves a widget if it's already in the layout.
        # We just need to iterate through the visible items and place them in the correct grid cell.
        for visible_idx, label_widget in self._labels.items():
            row, col = divmod(visible_idx, self._columns)
            self.grid_layout.addWidget(label_widget, row, col)
    
    def add_widget(self, index: int, widget: 'ThumbnailLabel'):
        """Add a widget to the layout at the specified index"""
        self._labels[index] = widget
        row, col = divmod(index, self._columns)
        self.grid_layout.addWidget(widget, row, col, 1, 1)
    
    def remove_widget(self, index: int):
        """Remove a widget from the layout"""
        if index in self._labels:
            widget = self._labels[index]
            self.grid_layout.removeWidget(widget)
            del self._labels[index]
    
    def clear_layout(self):
        """Clear all widgets from the layout"""
        # Remove widgets from layout but keep them parented to grid_container
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                # Don't set parent to None - just remove from layout
                pass
        # Don't clear labels here - let the calling code manage them
    
    def get_widget_at_position(self, pos: QPoint) -> Optional[int]:
        """Get the widget index at a given point inside the container."""
        # Position is relative to the grid_container, which has internal padding.
        adjusted_x = pos.x() - self.spacing
        adjusted_y = pos.y() - self.spacing

        if adjusted_x < 0 or adjusted_y < 0:
            return None # In top or left padding

        cell_dim = self.thumbnail_size + self.spacing
        col = int(adjusted_x // cell_dim)
        row = int(adjusted_y // cell_dim)

        # Check if in the gap between items
        if (adjusted_x % cell_dim) > self.thumbnail_size:
            return None
        if (adjusted_y % cell_dim) > self.thumbnail_size:
            return None

        if not (0 <= col < self._columns):
            return None

        visible_idx = row * self._columns + col
        if 0 <= visible_idx < len(self._current_files):
            return visible_idx

        return None
    
    def ensure_widget_visible(self, index: int, center: bool = False):
        """Ensure the widget at the given index is visible"""
        if index not in self._labels:
            return
            
        widget = self._labels[index]
        
        if center:
            # Get the viewport and widget geometry
            viewport = self.scroll_area.viewport()
            viewport_height = viewport.height()
            viewport_width = viewport.width()

            # Get widget's global position relative to grid container
            widget_pos = widget.mapTo(self.grid_container, QPoint(0, 0))

            # Calculate scroll positions to center the widget
            x = max(0, widget_pos.x() - (viewport_width - widget.width()) // 2)
            y = max(0, widget_pos.y() - (viewport_height - widget.height()) // 2)

            # Set scroll positions
            self.scroll_area.horizontalScrollBar().setValue(x)
            self.scroll_area.verticalScrollBar().setValue(y)
        else:
            # Just ensure the widget is visible
            self.scroll_area.ensureWidgetVisible(widget)
            
    def get_visible_rows(self) -> Tuple[int, int]:
        """Calculates first and last visible rows, accounting for scroll position and layout offsets."""
        row_height = self.thumbnail_size + self.spacing
        if row_height <= 0:
            return 0, -1 # Return invalid range if row height is non-positive

        # Y-offset of the grid container inside the viewport widget due to vertical centering.
        grid_offset_y = self.grid_container.y()

        scroll_y = self.scroll_area.verticalScrollBar().value()
        viewport_height = self.scroll_area.viewport().height()

        # Calculate the visible Y-range relative to the grid container's coordinate system.
        # This accounts for both scrolling and the centering offset.
        visible_y_start = scroll_y - grid_offset_y
        visible_y_end = visible_y_start + viewport_height

        # Find the row index at the top of the viewport. The content starts after `self.spacing`.
        first_visible_row = max(0, floor((visible_y_start - self.spacing) / row_height))

        # Find the row index at the bottom of the viewport.
        last_visible_row = max(0, floor((visible_y_end - self.spacing) / row_height))

        return int(first_visible_row), int(last_visible_row)

    def get_position(self, index: int) -> Tuple[int, int]:
        """Get row, column position for given index"""
        return divmod(index, self._columns)

    @property
    def columns(self) -> int:
        return self._columns
