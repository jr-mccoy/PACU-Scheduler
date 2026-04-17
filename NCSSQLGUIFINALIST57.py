"""Compatibility layer for legacy imports during GUI package transition."""

from ui import *  # noqa: F401,F403
from ui import App, UiStyle


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    UiStyle.apply(app)
    win = App()
    win.showMaximized()
    sys.exit(app.exec())
