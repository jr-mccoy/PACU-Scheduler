"""Compatibility shim — the legacy CLI presenter has moved to the ``cli`` package.

Importing from this module is deprecated; use ``cli.NurseSchedulerUI`` /
``cli.VisualCalendarUI`` directly.
"""

from warnings import warn as _warn

from cli.nurse_scheduler_ui import NurseSchedulerUI, VisualCalendarUI

_warn(
    "scheduler.ui_legacy is deprecated; import NurseSchedulerUI / "
    "VisualCalendarUI from the top-level `cli` package instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "VisualCalendarUI",
    "NurseSchedulerUI",
]
