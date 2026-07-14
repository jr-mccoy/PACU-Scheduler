"""Stable public API for the PACU scheduling backend.

Implementations are owned by focused modules; no public package import routes
through the deprecated ``legacy_core`` compatibility facade.
"""

from .debug import (
    ASSIGNMENT_DEBUG_LOGGER,
    AssignmentDebugLogger,
    _accept,
    _dbg_pairs,
    _dbg_variants,
    _open_dbg,
    _pair,
    _reject,
    configure_assignment_debug_logger,
    configure_pair_variant_debug,
    log,
)
from .debug import _DEBUG, _LOG_FILE_CACHE
from .domain import (
    BestStateTracker,
    Comparison,
    NurseManagerProtocol,
    PreSchedulerProtocol,
    Role,
    ScheduleQuality,
    ScheduleState,
    ScheduleVariant,
    SchedulerConfig,
    StateSnapshot,
    WeekBackup,
    WeekendAssignment,
    WeekendHistoryProtocol,
    WeekendPattern,
)
from .engine import (
    WORKER_TUNING,
    NurseScheduler,
    WorkerTuningConfig,
    _evaluate_variant_worker,
    _evaluate_variant_worker_profiled,
)
from .factory import (
    BackendService,
    SchedulerService,
    build_scheduler_config_from_settings,
    build_scheduler_from_settings,
    build_scheduler_service,
)
from .history_services import ViolationHistoryService, WeekendHistoryService
from .platform import allow_sleep, inhibit_sleep
from .profiling import (
    MetricsCollector,
    PerformanceProfiler,
    PerformanceReport,
    PhaseMetrics,
    WorkerMetrics,
)
from .repositories import (
    AssignmentHistory,
    DBColumns,
    DBTables,
    DatabaseMixin,
    DateUtils,
    NurseManager,
    PreScheduler,
    WeekendHistory,
)
from .runtime import is_empty
from .settings import SharedSettings


__all__ = [
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "AssignmentHistory",
    "BackendService",
    "BestStateTracker",
    "Comparison",
    "DBColumns",
    "DBTables",
    "DatabaseMixin",
    "DateUtils",
    "MetricsCollector",
    "NurseManager",
    "NurseManagerProtocol",
    "NurseScheduler",
    "PerformanceProfiler",
    "PerformanceReport",
    "PhaseMetrics",
    "PreScheduler",
    "PreSchedulerProtocol",
    "Role",
    "ScheduleQuality",
    "SchedulerConfig",
    "SchedulerService",
    "ScheduleState",
    "ScheduleVariant",
    "SharedSettings",
    "StateSnapshot",
    "ViolationHistoryService",
    "WeekBackup",
    "WeekendAssignment",
    "WeekendHistory",
    "WeekendHistoryProtocol",
    "WeekendHistoryService",
    "WeekendPattern",
    "WORKER_TUNING",
    "WorkerMetrics",
    "WorkerTuningConfig",
    "_DEBUG",
    "_LOG_FILE_CACHE",
    "_accept",
    "_dbg_pairs",
    "_dbg_variants",
    "_evaluate_variant_worker",
    "_evaluate_variant_worker_profiled",
    "_open_dbg",
    "_pair",
    "_reject",
    "allow_sleep",
    "build_scheduler_config_from_settings",
    "build_scheduler_from_settings",
    "build_scheduler_service",
    "configure_assignment_debug_logger",
    "configure_pair_variant_debug",
    "inhibit_sleep",
    "is_empty",
    "log",
]


_DEPRECATED_CLI_REEXPORTS = {
    "CLIHelper": ("cli.nurse_scheduler_ui", "CLIHelper"),
    "InputValidator": ("cli.nurse_scheduler_ui", "InputValidator"),
    "NurseSchedulerUI": ("cli.nurse_scheduler_ui", "NurseSchedulerUI"),
    "VisualCalendarUI": ("cli.nurse_scheduler_ui", "VisualCalendarUI"),
}


def __getattr__(name: str):  # pragma: no cover - compatibility only
    if name in _DEPRECATED_CLI_REEXPORTS:
        import importlib
        import warnings

        module_name, attribute = _DEPRECATED_CLI_REEXPORTS[name]
        warnings.warn(
            f"scheduler.{name} has moved to {module_name}.{attribute}; "
            "import it from the `cli` package instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module 'scheduler' has no attribute {name!r}")
