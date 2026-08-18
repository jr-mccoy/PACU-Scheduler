"""Deprecated compatibility facade for the former backend monolith.

Concrete implementations now live in focused scheduler modules.  This module
only preserves old import paths and a handful of patchable test/debug knobs.
"""

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
from .domain import *
from .domain import __all__ as _domain_all
from .engine import (
    WORKER_TUNING,
    NurseScheduler,
    WorkerTuningConfig,
    _evaluate_variant_core,
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
from .platform import allow_sleep, inhibit_sleep
from .profiling import *
from .profiling import __all__ as _profiling_all
from .repositories import *
from .repositories import __all__ as _repository_all
from .runtime import (
    ANALYSE_INITIAL_WEEKDAY_GAPS,
    GAP_REPORT_FILE,
    MEASURE_PHASE_TIMES,
    PERFORMANCE_PROFILE_JSON_DEFAULT,
    PERFORMANCE_PROFILING_REQUESTED,
    _count_main_backup_empties,
    _count_weekday_gaps,
    _main_backup_empty_mask,
    is_empty,
)
from .settings import SharedSettings

__all__ = list(dict.fromkeys([
    *_domain_all,
    *_repository_all,
    *_profiling_all,
    "SharedSettings",
    "WorkerTuningConfig",
    "WORKER_TUNING",
    "NurseScheduler",
    "_evaluate_variant_core",
    "_evaluate_variant_worker",
    "_evaluate_variant_worker_profiled",
    "AssignmentDebugLogger",
    "ASSIGNMENT_DEBUG_LOGGER",
    "_open_dbg",
    "_dbg_pairs",
    "_dbg_variants",
    "_reject",
    "_accept",
    "_pair",
    "log",
    "BackendService",
    "SchedulerService",
    "build_scheduler_config_from_settings",
    "build_scheduler_from_settings",
    "build_scheduler_service",
    "inhibit_sleep",
    "allow_sleep",
    "is_empty",
]))
