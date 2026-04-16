"""Domain models for the nurse scheduler.

This module is the first extraction target from the legacy monolith.
For compatibility during migration, symbols are currently re-exported
from :mod:`scheduler.legacy_core`.
"""

from .legacy_core import (
    WeekendPattern,
    SchedulerConfig,
    ScheduleState,
    ScheduleVariant,
    ScheduleQuality,
)

__all__ = [
    "WeekendPattern",
    "SchedulerConfig",
    "ScheduleState",
    "ScheduleVariant",
    "ScheduleQuality",
]
