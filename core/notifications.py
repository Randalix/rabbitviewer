"""Notification dataclasses shared between core/ and GUI.

These types were previously defined in network/protocol.py. They now live
here so that core/ has no dependency on the network layer.
"""
import dataclasses
import typing
from typing import Any, Dict, List, Optional


def _model_validate(cls, data: dict):
    """Construct a dataclass from a dict, coercing nested ImageEntryModel."""
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        hint = hints.get(f.name)
        origin = getattr(hint, '__origin__', None)
        if origin is list and val:
            inner = getattr(hint, '__args__', (None,))[0]
            if inner and isinstance(inner, type) and issubclass(inner, ImageEntryModel):
                val = [ImageEntryModel(**v) if isinstance(v, dict)
                       else ImageEntryModel(path=v) if isinstance(v, str)
                       else v for v in val]
        elif isinstance(hint, type) and hint is ImageEntryModel and isinstance(val, dict):
            val = ImageEntryModel(**val)
        kwargs[f.name] = val
    return cls(**kwargs)


@dataclasses.dataclass
class ImageEntryModel:
    """Wire-format representation of an ImageEntry."""
    path: str = ""
    sidecars: List[str] = dataclasses.field(default_factory=list)
    variant: Optional[str] = None

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class Notification:
    """Notification emitted by core workers and consumed by the GUI."""
    type: str = ""
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class ScanCompleteData:
    path: str = ""
    file_count: int = 0
    files: List[ImageEntryModel] = dataclasses.field(default_factory=list)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class ScanProgressData:
    path: str = ""
    files: List[ImageEntryModel] = dataclasses.field(default_factory=list)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class PreviewsReadyData:
    image_entry: ImageEntryModel = dataclasses.field(default_factory=ImageEntryModel)
    thumbnail_path: Optional[str] = None
    view_image_path: Optional[str] = None
    view_image_source: Optional[str] = None  # "disk", "memory", or None

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class FilesRemovedData:
    files: List[ImageEntryModel] = dataclasses.field(default_factory=list)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class ClipIndexProgressData:
    directory: str = ""
    completed: int = 0
    total: int = 0

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class FaceDetectionProgressData:
    directory: str = ""
    completed: int = 0
    total: int = 0

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)


@dataclasses.dataclass
class ComfyUICompleteData:
    source_path: str = ""
    result_path: str = ""
    status: str = "success"  # "success" or "error"
    error: str = ""

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def model_validate(cls, data: dict):
        return _model_validate(cls, data)
