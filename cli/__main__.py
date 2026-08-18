"""Entry point for ``python -m cli`` — boots the terminal menu loop."""

from scheduler.logging_config import configure_logging

from .nurse_scheduler_ui import main

if __name__ == "__main__":
    configure_logging()
    main()
