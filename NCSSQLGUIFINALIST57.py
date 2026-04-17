"""Compatibility layer for legacy imports during GUI package transition."""

from warnings import warn as _warn

from ui import App, UiStyle

_warn(
    "NCSSQLGUIFINALIST57 is deprecated and will be removed in a future release. "
    "Import from ui instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["App", "UiStyle"]


def _verify_backend_imports() -> None:
    """Fail fast if the stable scheduler backend API is incomplete."""
    from scheduler import (
        NurseScheduler,
        SchedulerConfig,
        SharedSettings,
        build_scheduler_from_settings,
    )

    expected = [
        NurseScheduler,
        SchedulerConfig,
        SharedSettings,
        build_scheduler_from_settings,
    ]
    if not all(expected):  # pragma: no cover - import smoke guard
        raise RuntimeError("scheduler backend public API import smoke test failed")


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    _verify_backend_imports()

    app = QApplication(sys.argv)
    UiStyle.apply(app)
    win = App()
    win.showMaximized()
    sys.exit(app.exec())
