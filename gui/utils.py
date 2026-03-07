from PySide6.QtWidgets import QApplication


def mono_font() -> str:
    """Return the configured monospace font family, or the CSS generic fallback.

    Must be called from the GUI thread (reads a QApplication property).
    """
    app = QApplication.instance()
    return (app.property("monospace_font") if app else None) or "monospace"
