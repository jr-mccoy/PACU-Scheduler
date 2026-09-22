"""Stable public API for the PACU scheduling backend.

Implementations are owned by focused modules; no public package import routes
through the deprecated ``legacy_core`` compatibility facade.
"""

from .apply import ApplyReport, apply_schedule
from .debug import (
    _DEBUG,
    _LOG_FILE_CACHE,
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
from .domain import (
    DEFAULT_MAX_WEEKEND_VARIANTS,
    BestStateTracker,
    Comparison,
    NurseManagerProtocol,
    PreSchedulerProtocol,
    Role,
    ScheduleQuality,
    SchedulerConfig,
    ScheduleState,
    ScheduleVariant,
    StateSnapshot,
    WeekBackup,
    WeekendAssignment,
    WeekendHistoryProtocol,
    WeekendPattern,
)
from .engine import (
    WORKER_TUNING,
    GenerationError,
    NurseScheduler,
    WeekendGenerationResult,
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
from .platform import allow_sleep, default_worker_count, inhibit_sleep
from .profiling import (
    MetricsCollector,
    PerformanceProfiler,
    PerformanceReport,
    PhaseMetrics,
    WorkerMetrics,
)
from .repositories import (
    AssignmentHistory,
    DatabaseMixin,
    DateUtils,
    DBColumns,
    DBTables,
    NurseManager,
    PreScheduler,
    WeekendHistory,
    ensure_schema,
)
from .runtime import is_empty
from .settings import MAX_WEEKEND_VARIANTS_RANGE, SharedSettings

__all__ = [
    "GenerationError",
    "WeekendGenerationResult",
    "ApplyReport",
    "apply_schedule",
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "AssignmentHistory",
    "BackendService",
    "BestStateTracker",
    "Comparison",
    "DBColumns",
    "DEFAULT_MAX_WEEKEND_VARIANTS",
    "DBTables",
    "DatabaseMixin",
    "DateUtils",
    "MAX_WEEKEND_VARIANTS_RANGE",
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
    "default_worker_count",
    "ensure_schema",
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
