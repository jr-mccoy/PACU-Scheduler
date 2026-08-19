"""Desktop entrypoint for the PACU scheduler.

Launches the Qt application. For the terminal interface, run ``python -m cli``.
"""

from __future__ import annotations

import sys

from scheduler.logging_config import configure_logging
from ui import App, UiStyle


def main() -> int:
    configure_logging()

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    UiStyle.apply(app)
    window = App()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
