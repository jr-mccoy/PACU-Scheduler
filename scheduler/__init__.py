"""Stable backend entrypoint for scheduler consumers.

Public API
==========
The GUI and external callers should import backend symbols from this package
instead of versioned module filenames.

Core scheduling API relied on by the GUI:

- ``NurseScheduler``
- ``SchedulerConfig``
- ``build_scheduler_from_settings``
- ``build_scheduler_config_from_settings``
- ``SharedSettings``
- ``NurseManager``
- ``PreScheduler``
- ``AssignmentHistory``
- ``WeekendHistory``
- ``WeekendPattern``
- ``NurseSchedulerUI``
- ``_evaluate_variant_worker``

Debug helpers used by legacy GUI wiring:

- ``AssignmentDebugLogger``
- ``ASSIGNMENT_DEBUG_LOGGER``
- ``_open_dbg``
"""

from .domain import (
    WeekendPattern,
    SchedulerConfig,
    ScheduleState,
    ScheduleVariant,
    ScheduleQuality,
)
from .repositories import DatabaseMixin, WeekendHistory, NurseManager, AssignmentHistory
from .engine import NurseScheduler, _evaluate_variant_worker, _evaluate_variant_worker_profiled
from .profiling import (
    PhaseMetrics,
    WorkerMetrics,
    PerformanceProfiler,
    MetricsCollector,
    PerformanceReport,
)
from .debug import _open_dbg, _dbg_pairs, _dbg_variants, _reject, _accept, _pair
from .history_services import WeekendHistoryService, ViolationHistoryService
from .legacy_core import (
    PreScheduler,
    NurseSchedulerUI,
    VisualCalendarUI,
    SharedSettings,
    build_scheduler_from_settings,
    build_scheduler_config_from_settings,
    AssignmentDebugLogger,
    ASSIGNMENT_DEBUG_LOGGER,
)

__all__ = [
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "AssignmentHistory",
    "DatabaseMixin",
    "MetricsCollector",
    "NurseManager",
    "NurseScheduler",
    "NurseSchedulerUI",
    "PerformanceProfiler",
    "PerformanceReport",
    "PhaseMetrics",
    "PreScheduler",
    "ScheduleQuality",
    "SchedulerConfig",
    "ScheduleState",
    "ScheduleVariant",
    "SharedSettings",
    "VisualCalendarUI",
    "ViolationHistoryService",
    "WeekendHistory",
    "WeekendHistoryService",
    "WeekendPattern",
    "WorkerMetrics",
    "_accept",
    "_dbg_pairs",
    "_dbg_variants",
    "_evaluate_variant_worker",
    "_evaluate_variant_worker_profiled",
    "_open_dbg",
    "_pair",
    "_reject",
    "build_scheduler_config_from_settings",
    "build_scheduler_from_settings",
]
