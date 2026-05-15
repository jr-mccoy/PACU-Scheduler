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
- ``build_scheduler_service`` / ``SchedulerService`` / ``BackendService``
- ``SharedSettings``
- ``NurseManager``
- ``PreScheduler``
- ``AssignmentHistory``
- ``WeekendHistory``
- ``WeekendPattern``
- ``_evaluate_variant_worker``

Debug helpers used by legacy GUI wiring:

- ``AssignmentDebugLogger``
- ``ASSIGNMENT_DEBUG_LOGGER``
- ``_open_dbg``

Deprecated (will be removed in a future release):

- ``NurseSchedulerUI`` and ``VisualCalendarUI`` now live in the top-level
  ``cli`` package. Importing them from ``scheduler`` still works but emits
  a :class:`DeprecationWarning`.
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
from .debug import (
    _open_dbg,
    _dbg_pairs,
    _dbg_variants,
    _reject,
    _accept,
    _pair,
    configure_pair_variant_debug,
    configure_assignment_debug_logger,
)
from .history_services import WeekendHistoryService, ViolationHistoryService
from .legacy_core import (
    PreScheduler,
    SharedSettings,
    build_scheduler_from_settings,
    build_scheduler_config_from_settings,
    AssignmentDebugLogger,
    ASSIGNMENT_DEBUG_LOGGER,
)
from .factory import BackendService, SchedulerService, build_scheduler_service


__all__ = [
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "AssignmentHistory",
    "BackendService",
    "DatabaseMixin",
    "MetricsCollector",
    "NurseManager",
    "NurseScheduler",
    "PerformanceProfiler",
    "PerformanceReport",
    "PhaseMetrics",
    "PreScheduler",
    "ScheduleQuality",
    "SchedulerConfig",
    "SchedulerService",
    "ScheduleState",
    "ScheduleVariant",
    "SharedSettings",
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
    "build_scheduler_service",
    "configure_assignment_debug_logger",
    "configure_pair_variant_debug",
]


_DEPRECATED_CLI_REEXPORTS = {
    "NurseSchedulerUI": ("cli.nurse_scheduler_ui", "NurseSchedulerUI"),
    "VisualCalendarUI": ("cli.nurse_scheduler_ui", "VisualCalendarUI"),
}


def __getattr__(name: str):  # pragma: no cover - thin deprecation shim
    """Lazy re-export of CLI classes for callers that haven't migrated yet."""
    if name in _DEPRECATED_CLI_REEXPORTS:
        import importlib
        import warnings

        module_name, attr = _DEPRECATED_CLI_REEXPORTS[name]
        warnings.warn(
            f"scheduler.{name} has moved to {module_name}.{attr}; "
            "import it from the `cli` package instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    raise AttributeError(f"module 'scheduler' has no attribute {name!r}")
