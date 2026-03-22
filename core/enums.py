from enum import Enum


class FileOperation(Enum):
    DELETE = 'delete'
    MOVE = 'move'
    COPY = 'copy'
