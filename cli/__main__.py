"""Entry point for ``python -m cli`` — boots the terminal menu loop."""

from scheduler.logging_config import configure_logging

from .nurse_scheduler_ui import main as _run_menu_loop


def run() -> None:
    """Configure logging, then hand control to the menu loop."""
    configure_logging()
    _run_menu_loop()


if __name__ == "__main__":
    run()
