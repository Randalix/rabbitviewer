from .content_provider import ContentProvider, Section
from .metadata_provider import MetadataProvider
from .script_output_provider import ScriptOutputProvider


def __getattr__(name):
    if name == "InfoPanelShell":
        from .info_panel_shell import InfoPanelShell
        return InfoPanelShell
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
