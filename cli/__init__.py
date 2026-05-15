"""Command-line interface package for the Nurse Scheduler.

This package contains the legacy text-mode user interface that previously
lived inside ``scheduler.legacy_core``. Moving it here keeps the
``scheduler`` package free of ``input()``/``print()`` driven code paths so
the GUI can compose backend services without dragging in CLI helpers.
"""

from .nurse_scheduler_ui import (
    CLIHelper,
    InputValidator,
    NurseSchedulerUI,
    VisualCalendarUI,
    main,
)

__all__ = [
    "CLIHelper",
    "InputValidator",
    "NurseSchedulerUI",
    "VisualCalendarUI",
    "main",
]
