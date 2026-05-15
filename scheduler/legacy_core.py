from __future__ import annotations

#pylint:disable=W0718
#pylint:disable=W0611
#pylint:disable=W1203
from datetime import date, timedelta
import os
import calendar
import logging
import copy
import sqlite3
import datetime
from enum import Enum
from typing import (
    List,
    Dict,
    Optional,
    Tuple,
    Any,
    Union,
    Set,
    Protocol,
    Iterator,
    NamedTuple,
    Iterable,
    Callable,
    TYPE_CHECKING,
)
import traceback
import pandas as pd
from copy import deepcopy
from itertools import permutations
import bisect
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm
import time
import atexit
import threading
import json
import pathlib
import csv
import sys, subprocess, platform, shutil
from dataclasses import dataclass, field
from functools import cached_property
import numpy as np      # needed for median in long-term score helpers

try:  # Optional dependency used for performance profiling
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil may be unavailable in minimal envs
    psutil = None
# https://pastebin.com/m29cSCp5
from collections import defaultdict
from contextlib import contextmanager
import random
from .constraints import (
    WeekdayConstraintConfig,
    passes_basic_eligibility_checks,
    validate_post_weekend_assignment,
    validate_weekday_relative_to_weekend,
    validate_weekday_relative_to_weekend_gap,
)
from .assignment import apply_assignment, revert_assignment, AssignmentMutation
from .scoring import (
    QualityMetrics,
    QualityComparison,
    compare_quality,
    compute_quality_metrics,
    weighted_scores_from_rows,
)
from .generation import CandidateDomainBuilder, OrderGenerator
from .optimization import WindowRefillOptimizer
from .history_services import WeekendHistoryService, ViolationHistoryService
from .exporters.pdf import (
    DEFAULT_PDF_FONT_SIZES as _DEFAULT_PDF_FONT_SIZES,
    draw_week_rows as _draw_week_rows_fn,
    draw_weekday_header as _draw_weekday_header_fn,
    export_variant_pdf as _export_variant_pdf_fn,
)

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Turn this off when you no longer need the timing info
MEASURE_PHASE_TIMES = True

ANALYSE_INITIAL_WEEKDAY_GAPS = True
GAP_REPORT_FILE = "weekday_gap_report.txt"


def _env_flag(name: str, default: bool = False) -> bool:
    """Return True if the environment variable ``name`` evaluates to truthy."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PERFORMANCE_PROFILING_REQUESTED = _env_flag("NSCHED_PROFILE", False)
PERFORMANCE_PROFILE_JSON_DEFAULT = os.environ.get(
    "NSCHED_PROFILE_JSON", "performance_metrics.json"
)

# ───────────────
# DEBUG FILE HELPERS
# ───────────────

_DBG_MODE = os.environ.get("NSCHED_DEBUG", "").lower()        # '', pairs, variants, all

def _open_dbg(path: str, mode: str = "a"):
    """Safely open *path* for debug logging, tolerating OS lock failures."""

    try:
        return open(path, mode, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[debug] unable to open {path!r}: {exc}")
        return None

_DBG_FILE_PAIRS    = _open_dbg("debug_pairs.txt")    if _DBG_MODE in ("pairs", "all")     else None
_DBG_FILE_VARIANTS = _open_dbg("debug_variants.txt") if _DBG_MODE in ("variants", "all")  else None

# Make sure we never leak the handles
for _fh in (_DBG_FILE_PAIRS, _DBG_FILE_VARIANTS):
    if _fh:
        atexit.register(_fh.close)

# ----------------------------------
# convenience writers
# ----------------------------------
def _dbg_pairs(msg: str = "") -> None:
    if _DBG_FILE_PAIRS:
        _DBG_FILE_PAIRS.write(msg + "\n")

def _dbg_variants(msg: str = "") -> None:
    if _DBG_FILE_VARIANTS:
        _DBG_FILE_VARIANTS.write(msg + "\n")

def _reject(nurse: str, reason: str) -> None:
    _dbg_pairs(f"    ✗ {nurse:15}  [{reason}]")

def _accept(nurse: str, tag: str) -> None:
    _dbg_pairs(f"    ✓ {nurse:15}  [{tag}]")

def _pair(fsf: str, sfs: str) -> None:
    _dbg_pairs(f"    → PAIR: {fsf:10} + {sfs}")
    
def _count_weekday_gaps(schedule_df) -> int:
    """
    Return number of empty cells (main + backup) Mon–Thu only,
    treating None/NaN/blank as empty using is_empty().
    """
    return _count_main_backup_empties(schedule_df, weekdays_only=True)

def is_empty(value) -> bool:
    """
    Check if a given value is considered empty or unassigned.
    Returns True if the value is None, NaN, empty string, or whitespace-only string.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    na_value = pd.isna(value)
    if isinstance(na_value, (bool, np.bool_)):
        return bool(na_value)
    return False


def _main_backup_empty_mask(
    schedule_df: pd.DataFrame, *, weekdays_only: bool = False
) -> pd.DataFrame:
    """Return a boolean empty-mask for ``main``/``backup`` cells."""
    if weekdays_only:
        day_mask = ~schedule_df["is_weekend"]  # Mon–Thu
        sub = schedule_df.loc[day_mask, ["main", "backup"]]
    else:
        sub = schedule_df[["main", "backup"]]
    mapper = getattr(sub, "map", None)  # pandas >= 2.1.0
    return mapper(is_empty) if callable(mapper) else sub.applymap(is_empty)


def _count_main_backup_empties(
    schedule_df: pd.DataFrame, *, weekdays_only: bool = False
) -> int:
    """Count empty ``main``/``backup`` cells using ``is_empty`` semantics."""
    return int(_main_backup_empty_mask(schedule_df, weekdays_only=weekdays_only).to_numpy().sum())

@dataclass
class PhaseMetrics:
    """Metrics for a single phase of execution."""

    duration_sec: float
    cpu_percent_start: float
    cpu_percent_end: float
    memory_mb_start: float
    memory_mb_end: float
    memory_mb_delta: float

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 4),
            "cpu_start": round(self.cpu_percent_start, 1),
            "cpu_end": round(self.cpu_percent_end, 1),
            "mem_start_mb": round(self.memory_mb_start, 1),
            "mem_end_mb": round(self.memory_mb_end, 1),
            "mem_delta_mb": round(self.memory_mb_delta, 1),
        }


@dataclass
class WorkerMetrics:
    """Complete metrics for one variant evaluation."""

    worker_id: int
    variant_idx: int
    phases: Dict[str, PhaseMetrics] = field(default_factory=dict)
    total_duration_sec: float = 0.0
    process_id: int = 0
    cpu_count: int = 0

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "variant_idx": self.variant_idx,
            "process_id": self.process_id,
            "cpu_count": self.cpu_count,
            "total_duration_sec": round(self.total_duration_sec, 4),
            "phases": {name: metrics.to_dict() for name, metrics in self.phases.items()},
        }


class PerformanceProfiler:
    """Context manager for profiling a single phase."""

    def __init__(self, phase_name: str, metrics_collector: "MetricsCollector"):
        if psutil is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("psutil is required for performance profiling.")
        self.phase_name = phase_name
        self.collector = metrics_collector
        self.start_time = 0.0
        self.start_cpu = 0.0
        self.start_mem = 0.0
        self.process = psutil.Process()

    def __enter__(self):
        self.process.cpu_percent(interval=None)
        time.sleep(0.001)

        self.start_time = time.perf_counter()
        self.start_cpu = self.process.cpu_percent(interval=None)
        self.start_mem = self.process.memory_info().rss / (1024 * 1024)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = time.perf_counter()
        end_cpu = self.process.cpu_percent(interval=None)
        end_mem = self.process.memory_info().rss / (1024 * 1024)

        metrics = PhaseMetrics(
            duration_sec=end_time - self.start_time,
            cpu_percent_start=self.start_cpu,
            cpu_percent_end=end_cpu,
            memory_mb_start=self.start_mem,
            memory_mb_end=end_mem,
            memory_mb_delta=end_mem - self.start_mem,
        )

        self.collector.add_phase_metrics(self.phase_name, metrics)
        return False


class MetricsCollector:
    """Collect metrics for a single worker."""

    def __init__(self, worker_id: int, variant_idx: int):
        self.metrics = WorkerMetrics(
            worker_id=worker_id,
            variant_idx=variant_idx,
            process_id=os.getpid(),
            cpu_count=os.cpu_count() or 1,
        )
        self.start_time = time.perf_counter()

    def add_phase_metrics(self, phase_name: str, metrics: PhaseMetrics):
        self.metrics.phases[phase_name] = metrics

    def finalize(self) -> WorkerMetrics:
        self.metrics.total_duration_sec = time.perf_counter() - self.start_time
        return self.metrics

    def profile_phase(self, phase_name: str) -> PerformanceProfiler:
        return PerformanceProfiler(phase_name, self)


@dataclass(frozen=True)
class WorkerTuningConfig:
    """Immutable algorithm tuning shared by all worker evaluation paths."""

    gap_fill_iterations: int = 300
    rebalance_tolerance: int = 1
    rebalance_iterations: int = 1500
    rebalance_early_stop_spread: Optional[tuple[int, int]] = None
    window_refill_weeks: int = 3
    window_refill_max_passes: int = 650
    window_refill_time_limit_ms: int = 800000
    window_refill_node_limit: int = 750000
    window_refill_target_spread: tuple[int, int] = (1, 1)
    full_period_max_orders: int = 1000
    full_period_per_attempt_time_ms: int = 800000
    full_period_per_attempt_nodes: int = 1500000
    full_period_target_spread: tuple[int, int] = (1, 1)


WORKER_TUNING = WorkerTuningConfig()


class PerformanceReport:
    """Aggregates and reports performance metrics across workers."""

    def __init__(self, all_metrics: List[WorkerMetrics]):
        self.all_metrics = all_metrics
        self.num_workers = len(all_metrics)

    def print_summary(self):
        if not self.all_metrics:
            print("No metrics collected.")
            return

        print("\n" + "=" * 80)
        print("PERFORMANCE PROFILING REPORT")
        print("=" * 80)

        total_time = sum(m.total_duration_sec for m in self.all_metrics)
        avg_time = total_time / len(self.all_metrics)
        min_time = min(m.total_duration_sec for m in self.all_metrics)
        max_time = max(m.total_duration_sec for m in self.all_metrics)

        print(f"\nOverall Summary:")
        print(f"  Workers evaluated: {len(self.all_metrics)}")
        print(f"  Total time (all workers): {total_time:.2f}s")
        print(f"  Average time per worker: {avg_time:.2f}s")
        print(f"  Min/Max worker time: {min_time:.2f}s / {max_time:.2f}s")
        print(f"  CPU cores available: {self.all_metrics[0].cpu_count}")

        print(f"\nPhase Breakdown (averaged across workers):")
        self._print_phase_summary()

        print(f"\nResource Usage:")
        self._print_resource_summary()

        print(f"\nPer-Worker Details:")
        self._print_worker_details()

        print("=" * 80 + "\n")

    def _print_phase_summary(self):
        all_phases: set[str] = set()
        for m in self.all_metrics:
            all_phases.update(m.phases.keys())

        if not all_phases:
            print("  No phase data available.")
            return

        phase_stats: dict[str, dict[str, float]] = {}
        for phase in sorted(all_phases):
            durations = [m.phases[phase].duration_sec for m in self.all_metrics if phase in m.phases]
            mem_deltas = [m.phases[phase].memory_mb_delta for m in self.all_metrics if phase in m.phases]

            if durations:
                phase_stats[phase] = {
                    "avg_time": sum(durations) / len(durations),
                    "min_time": min(durations),
                    "max_time": max(durations),
                    "avg_mem_delta": sum(mem_deltas) / len(mem_deltas),
                }

        for phase in sorted(phase_stats, key=lambda p: phase_stats[p]["avg_time"], reverse=True):
            stats = phase_stats[phase]
            print(
                f"  {phase:25} {stats['avg_time']:7.3f}s  "
                f"(min: {stats['min_time']:.3f}s, max: {stats['max_time']:.3f}s)  "
                f"[mem Δ: {stats['avg_mem_delta']:+.1f} MB]"
            )

    def _print_resource_summary(self):
        total_mem_deltas = [sum(p.memory_mb_delta for p in m.phases.values()) for m in self.all_metrics]
        if total_mem_deltas:
            avg_mem = sum(total_mem_deltas) / len(total_mem_deltas)
            max_mem = max(total_mem_deltas)
            print(f"  Average memory delta per worker: {avg_mem:+.1f} MB")
            print(f"  Max memory delta (single worker): {max_mem:+.1f} MB")

        peak_mems = [max((p.memory_mb_end for p in m.phases.values()), default=0) for m in self.all_metrics]
        if peak_mems:
            print(f"  Peak memory usage (max across workers): {max(peak_mems):.1f} MB")

    def _print_worker_details(self):
        for m in sorted(self.all_metrics, key=lambda x: x.worker_id):
            print(f"\n  Worker {m.worker_id} (Variant {m.variant_idx}, PID {m.process_id}):")
            print(f"    Total time: {m.total_duration_sec:.3f}s")

            if m.phases:
                print("    Phases:")
                for phase, metrics in sorted(m.phases.items()):
                    print(
                        f"      {phase:20} {metrics.duration_sec:7.3f}s  "
                        f"[mem: {metrics.memory_mb_start:.0f} → {metrics.memory_mb_end:.0f} MB, "
                        f"Δ {metrics.memory_mb_delta:+.1f} MB]"
                    )

    def export_json(self, filename: str):
        data = {
            "num_workers": self.num_workers,
            "workers": [m.to_dict() for m in self.all_metrics],
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Exported metrics to {filename}")

@contextmanager
def _noop_phase_timer(_phase_name: str):
    """Phase-timer no-op used when profiling is disabled."""
    yield


def _evaluate_variant_core(args, *, with_profiling: bool):
    """Shared implementation for variant evaluation.

    The profiled and non-profiled worker paths historically diverged into
    near-duplicate copies; both routed through the same algorithm but used
    different timing primitives.  This implementation runs the algorithm
    once and conditionally collects ``MetricsCollector`` data for callers
    that requested profiling.
    """
    idx, variant = args
    tuning = WORKER_TUNING

    if with_profiling:
        collector = MetricsCollector(worker_id=idx, variant_idx=idx)
        phase_timer = collector.profile_phase

        def _phase_duration(name: str) -> float:
            phase = collector.metrics.phases.get(name)
            return phase.duration_sec if phase else 0.0
    else:
        collector = None
        phase_timer = _noop_phase_timer
        t0 = time.perf_counter()
        phase_starts: dict[str, float] = {}
        phase_durations: dict[str, float] = {}

        @contextmanager
        def phase_timer(name: str):  # type: ignore[no-redef]
            start = time.perf_counter()
            phase_starts[name] = start
            try:
                yield
            finally:
                phase_durations[name] = time.perf_counter() - start

        def _phase_duration(name: str) -> float:
            return phase_durations.get(name, 0.0)

    try:
        with phase_timer("clone"):
            var = variant.clone()

        with phase_timer("assign_weekdays"):
            var.assign_weekdays()
            tracker = BestStateTracker(var)
            tracker.initialize()

        early_gaps = _count_weekday_gaps(var.state.schedule)

        with phase_timer("gap_fill"):
            var.iterative_gap_fill_no_revert(
                max_iterations=tuning.gap_fill_iterations,
                tracker=tracker,
            )

        with phase_timer("rebalance"):
            var.iterative_rebalance_no_revert(
                tolerance=tuning.rebalance_tolerance,
                max_iterations=tuning.rebalance_iterations,
                early_stop_spread=tuning.rebalance_early_stop_spread,
                tracker=tracker,
            )

        with phase_timer("window_refill"):
            var.iterative_window_refill_rebalance(
                window_weeks=tuning.window_refill_weeks,
                max_passes=tuning.window_refill_max_passes,
                time_limit_ms=tuning.window_refill_time_limit_ms,
                node_limit=tuning.window_refill_node_limit,
                target_spread=tuning.window_refill_target_spread,
                tracker=tracker,
            )

        s_b, s_m, _ = var._spread_components()
        if s_b > 1 or s_m > 1:
            with phase_timer("full_period_refill"):
                var.iterative_full_period_refill(
                    max_orders=tuning.full_period_max_orders,
                    per_attempt_time_ms=tuning.full_period_per_attempt_time_ms,
                    per_attempt_nodes=tuning.full_period_per_attempt_nodes,
                    target_spread=tuning.full_period_target_spread,
                    tracker=tracker,
                )

        tracker.restore_global_best()

        with phase_timer("compute_stats"):
            df = var.state.schedule
            final_quality = tracker.get_global_best_quality()

            if final_quality:
                gaps_final = final_quality.total_gaps
                balance_main = final_quality.main_spread
                balance_backup = final_quality.backup_spread
                rotation_rep = final_quality.rotation_penalty
            else:
                gaps_final = _count_main_backup_empties(df)
                main_counts = var.state.main_assignment_counts.values
                back_counts = var.state.backup_assignment_counts.values
                balance_main = int(main_counts.max() - main_counts.min()) if len(main_counts) else 0
                balance_backup = int(back_counts.max() - back_counts.min()) if len(back_counts) else 0
                rotation_rep = int(var.state.rotation_repeats)

            stats = {
                "gaps": int(gaps_final),
                "balance_main": int(balance_main),
                "balance_backup": int(balance_backup),
                "early_gaps": int(early_gaps),
                "rotation_rep": int(rotation_rep),
            }

            stats.update(tracker.get_statistics())

            nurse_counts: dict[str, dict[str, int]] = {}
            for nurse in var.nurses:
                main_count = int((df["main"] == nurse).sum())
                backup_count = int((df["backup"] == nurse).sum())
                nurse_counts[nurse] = {
                    "main": main_count,
                    "backup": backup_count,
                    "total": main_count + backup_count,
                }

            sched_copy = df.copy()

        if with_profiling:
            metrics = collector.finalize()
            total_duration = metrics.total_duration_sec
        else:
            metrics = None
            total_duration = time.perf_counter() - t0

        if MEASURE_PHASE_TIMES:
            stats.update(
                {
                    "t_clone": _phase_duration("clone"),
                    "t_assign": _phase_duration("assign_weekdays"),
                    "t_gapfill": _phase_duration("gap_fill"),
                    "t_rebalance": _phase_duration("rebalance"),
                    "t_lns_2w": _phase_duration("window_refill"),
                    "t_full": _phase_duration("full_period_refill"),
                    "t_total": total_duration,
                }
            )

        if with_profiling:
            return idx, stats, nurse_counts, sched_copy, metrics
        return idx, stats, nurse_counts, sched_copy

    except Exception:
        if with_profiling and collector is not None:
            collector.finalize()
            print(f"Worker {idx} failed during profiling")
        raise


def _evaluate_variant_worker(args):
    """Heavy lifting for one weekend variant.

    Returns ``(idx, stats, nurse_counts, schedule_df)`` with extra timing
    keys if :data:`MEASURE_PHASE_TIMES` is ``True``.
    """
    return _evaluate_variant_core(args, with_profiling=False)


def _evaluate_variant_worker_profiled(args):
    """Profiling-enabled variant evaluation used when profiling is requested.

    Returns ``(idx, stats, nurse_counts, schedule_df, worker_metrics)``.
    """
    return _evaluate_variant_core(args, with_profiling=True)

def build_scheduler_config_from_settings(settings) -> "SchedulerConfig":
    """
    Create a SchedulerConfig from AppSettings (or any settings-like object with .get()).
    """
    return SchedulerConfig(
        weekend_gap_days=settings.get("weekend_gap_days"),
        main_score_factor=settings.get("main_score_factor"),
        backup_score_factor=settings.get("backup_score_factor"),
        availability_penalty=settings.get("availability_penalty"),
        min_days_between_assignments=settings.get("min_days_between_assignments"),
        allow_post_weekend_wednesday_main=settings.get("allow_post_weekend_wednesday_main"),
        allow_post_weekend_wednesday_backup=settings.get("allow_post_weekend_wednesday_backup"),
        allow_post_weekend_thursday_main=settings.get("allow_post_weekend_thursday_main"),
        allow_post_weekend_thursday_backup=settings.get("allow_post_weekend_thursday_backup"),
        allow_one_day_weekday_gap=settings.get("allow_one_day_weekday_gap"),
        scoring_weights=settings.get("scoring_weights"),
    )

def build_scheduler_from_settings(
    start_date, end_date,
    nm: "NurseManager",
    wh: "WeekendHistory",
    ps: "PreScheduler",
    settings
) -> "NurseScheduler":
    """
    Build a NurseScheduler with deterministic nurse order and the same config/history_window_days
    the GUI uses. Both GUI and CLI call this.
    """
    cfg = build_scheduler_config_from_settings(settings)
    non_prn = sorted(nm.get_non_prn_nurses(), key=str.casefold)
    prn     = sorted(nm.get_prn_nurses(),     key=str.casefold)
    return NurseScheduler(
        start_date, end_date,
        non_prn, prn,
        nm, wh, ps,
        config=cfg,
        history_window_days=settings.get("history_window_days"),
    )


def inhibit_sleep():
    """
    Prevent the screen from going to sleep. Returns a handle
    you must pass to allow_sleep() when you’re done.
    """
    if sys.platform == "darwin":
        # macOS: caffeinate will keep display & system awake
        return subprocess.Popen(["caffeinate", "-dims"])
    elif sys.platform.startswith("win"):
        # Windows: SetThreadExecutionState DISPLAY_REQUIRED
        import ctypes
        ES_CONTINUOUS       = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_DISPLAY_REQUIRED
        )
        return None
    else:
        # Linux and others: no‐op (you could integrate a DBus inhibit here)
        return None

def _ensure_violation_table(db_name: str) -> None:
    """
    Create the tables that store rotation-violation counts, violation dates,
    and last/expected weekend patterns if they do not yet exist.
    """
    with sqlite3.connect(db_name) as conn:
        # Counts table with all columns required by readers/writers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rotation_violation_stats (
                nurse_id            INTEGER PRIMARY KEY,
                violation_count     INTEGER NOT NULL DEFAULT 0,
                last_violation_date TEXT,
                consec_violations   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (nurse_id) REFERENCES nurses (nurse_id)
            )
        """)

        # Violation dates table (unique nurse/date pairs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rotation_violation_dates (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                nurse_id         INTEGER NOT NULL,
                violation_date   TEXT NOT NULL,
                pattern          TEXT NOT NULL,
                previous_pattern TEXT NOT NULL,
                FOREIGN KEY (nurse_id) REFERENCES nurses (nurse_id),
                UNIQUE(nurse_id, violation_date)
            )
        """)

        # Last-pattern storage for each nurse
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekend_rotation_history (
                nurse_id              INTEGER PRIMARY KEY,
                last_pattern          TEXT,
                expected_next_pattern TEXT,
                FOREIGN KEY (nurse_id) REFERENCES nurses (nurse_id)
            )
        """)

def allow_sleep(handle):
    """
    Undo whatever inhibit_sleep() did.
    """
    if sys.platform == "darwin":
        if handle:
            handle.terminate()
    elif sys.platform.startswith("win"):
        import ctypes
        ES_CONTINUOUS = 0x80000000
        # drop the DISPLAY_REQUIRED bit
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    # else: nothing needed for Linux stub


class SharedSettings:
    """
    Read the GUI settings JSON (~/.nurse_scheduler/settings.json) so the CLI
    uses the same configuration. Falls back to known defaults if missing.
    """
    DEFAULTS = {
        "weekend_gap_days": 28,
        "min_days_between_assignments": 2,
        "main_score_factor": 10,
        "backup_score_factor": 10,
        "availability_penalty": 10,
        "history_window_days": 30,
        "allow_post_weekend_wednesday_main": False,
        "allow_post_weekend_wednesday_backup": True,
        "allow_post_weekend_thursday_main": True,
        "allow_post_weekend_thursday_backup": True,
        "allow_one_day_weekday_gap": False,
        "scoring_weights": {
            "rotation_rep": 0.30,
            "gaps":         0.20,
            "rot_viol":     0.15,
            "weekend_gap":  0.15,
            "balance":      0.10,
            "long_term":    0.10,
        },
    }

    def __init__(self, filename="settings.json"):
        base = os.path.expanduser("~")
        cfg_dir = os.path.join(base, ".nurse_scheduler")
        self.path = os.path.join(cfg_dir, filename)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def get(self, key):
        return self._settings.get(key, self.DEFAULTS[key])



logger = logging.getLogger(__name__)


class DatabaseMixin:
    """Mixin class to provide common database operations."""
    
    def __init__(self, db_name: str = 'nurse_schedule.db'):
        self.db_name = db_name
    
    @contextmanager
    def get_db_connection(self):
        """Context manager for database connections with error handling."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name)
            try:
                # Enforce foreign keys so future writes don't create orphans
                conn.execute('PRAGMA foreign_keys = ON')
            except Exception:
                # Older SQLite builds can be quirky—fail open rather than crash
                pass
            yield conn
        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            if conn:
                conn.rollback()
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.commit()
                conn.close()
    
    def execute_query(self, query: str, params: tuple = ()) -> list[tuple]:
        """Execute a SELECT query and return all results."""
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()
    
    def execute_single_query(self, query: str, params: tuple = ()) -> Optional[tuple]:
        """Execute a SELECT query and return single result."""
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()
    
    def execute_update(self, query: str, params: tuple = ()) -> None:
        """Execute an INSERT/UPDATE/DELETE query."""
        with self.get_db_connection() as conn:
            conn.execute(query, params)
            conn.commit()


class DateUtils:
    """Utility class for date operations."""
    
    @staticmethod
    def normalize_date(date_input) -> pd.Timestamp:
        """Normalize input date to pandas Timestamp at midnight."""
        try:
            return pd.to_datetime(date_input).normalize()
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to normalize date: {date_input}, error: {e}")
            raise ValueError(f"Invalid date format: {date_input}")
    
    @staticmethod
    def safe_normalize_date(date_input) -> Optional[pd.Timestamp]:
        """Safely normalize input date, returning None on failure."""
        try:
            normalized = DateUtils.normalize_date(date_input)
        except ValueError:
            logger.warning(f"Failed to normalize date: {date_input}")
            return None
        if pd.isna(normalized):
            logger.warning(f"Date normalization produced NaT for input: {date_input}")
            return None
        return normalized


class AssignmentHistory(DatabaseMixin):
    """Manages nurse assignment history with database persistence and caching."""
    
    # SQL queries as class constants
    LOAD_HISTORY_QUERY = '''
        SELECT sh.date, nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id=nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id=nb.nurse_id
        WHERE sh.date >= ?
    '''
    
    GET_HISTORY_BASE_QUERY = '''
        SELECT sh.date, nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id = nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id = nb.nurse_id
    '''
    
    UPDATE_HISTORY_QUERY = '''
        INSERT OR REPLACE INTO schedule_history (date, main_nurse_id, backup_nurse_id)
        VALUES (?, 
                (SELECT nurse_id FROM nurses WHERE name=?),
                (SELECT nurse_id FROM nurses WHERE name=?))
    '''
    
    GET_RECORD_QUERY = '''
        SELECT nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id=nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id=nb.nurse_id
        WHERE sh.date=?
    '''

    def __init__(self, db_name: str = 'nurse_schedule.db', history_duration_months: int = 6):
        super().__init__(db_name)
        self.history_duration_months = history_duration_months
        self._history = self._load_history()

    def _get_cutoff_date(self) -> pd.Timestamp:
        """Calculate the cutoff date for history retention."""
        return DateUtils.normalize_date(
            pd.Timestamp.today() - pd.DateOffset(months=self.history_duration_months)
        )

    def _load_history(self) -> Dict[pd.Timestamp, Dict[str, Optional[str]]]:
        """Load assignment history from the database."""
        history = {}
        cutoff_date = self._get_cutoff_date()
        cutoff_date_str = cutoff_date.strftime('%Y-%m-%d')

        results = self.execute_query(self.LOAD_HISTORY_QUERY, (cutoff_date_str,))
        
        for date_str, main, backup in results:
            normalized_date = DateUtils.normalize_date(date_str)
            history[normalized_date] = {"main": main, "backup": backup}
        
        return history

    def _refresh_cache(self) -> None:
        """Refresh the in-memory cache from database."""
        self._history = self._load_history()

    def get_all_history(self) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """Return all history records as a list of tuples."""
        return [
            (date.strftime('%Y-%m-%d'), data["main"], data["backup"])
            for date, data in sorted(self._history.items())
        ]

    def get_history(self, start_date=None, end_date=None) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """
        Retrieve assignment history records within an optional date range. 
        Args:
            start_date: Optional start date (inclusive). If None, no lower bound.
            end_date: Optional end date (inclusive). If None, no upper bound.    
        Returns:
            List of tuples containing (date_str, main_nurse, backup_nurse).
        """
        query = self.GET_HISTORY_BASE_QUERY
        conditions = []
        params = []
        
        if start_date:
            start = DateUtils.normalize_date(start_date).strftime('%Y-%m-%d')
            conditions.append("sh.date >= ?")
            params.append(start)
        
        if end_date:
            end = DateUtils.normalize_date(end_date).strftime('%Y-%m-%d')
            conditions.append("sh.date <= ?")
            params.append(end)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY sh.date ASC"
        
        results = self.execute_query(query, params)
        return [(date_str, main, backup) for date_str, main, backup in results]
    
    def get_counts(self, start_date, end_date) -> Tuple[Dict[str, int], Dict[str, int]]:
        """Get counts of main and backup assignments within a date range."""
        main_counts = {}
        backup_counts = {}
        start = DateUtils.normalize_date(start_date)
        end = DateUtils.normalize_date(end_date)
        
        for date, data in self._history.items():
            if start <= date <= end:
                main = data["main"]
                backup = data["backup"]
                if main:
                    main_counts[main] = main_counts.get(main, 0) + 1
                if backup:
                    backup_counts[backup] = backup_counts.get(backup, 0) + 1
        
        return main_counts, backup_counts

    def update_history(self, date_input, main_nurse: Optional[str], backup_nurse: Optional[str]) -> None:
        """Insert or update an assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime('%Y-%m-%d')
        
        self.execute_update(self.UPDATE_HISTORY_QUERY, (date_str, main_nurse, backup_nurse))
        
        # Update in-memory cache
        self._history[normalized_date] = {"main": main_nurse, "backup": backup_nurse}

    def get_record(self, date_input) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """Retrieve a specific assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime('%Y-%m-%d')
        
        return self.execute_single_query(self.GET_RECORD_QUERY, (date_str,))

    def delete_record(self, date_input) -> None:
        """Delete a specific assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime('%Y-%m-%d')
        
        self.execute_update('DELETE FROM schedule_history WHERE date=?', (date_str,))
        
        # Update in-memory cache
        self._history.pop(normalized_date, None)

    def prune_old_records(self) -> None:
        """Remove records older than the cutoff date."""
        cutoff_date = self._get_cutoff_date()
        cutoff_date_str = cutoff_date.strftime('%Y-%m-%d')
        
        self.execute_update("DELETE FROM schedule_history WHERE date < ?", (cutoff_date_str,))
        
        # Update in-memory cache
        self._history = {
            date: data for date, data in self._history.items()
            if date >= cutoff_date
        }


class WeekendPattern(str, Enum):
    """Weekend shift patterns."""
    FSF = "FSF"  # First-Second-First (Main on Friday, Backup on Saturday, Main on Sunday)
    SFS = "SFS"  # Second-First-Second (Backup on Friday, Main on Saturday, Backup on Sunday)


class WeekendAssignment:
    """Represents a weekend assignment with specific patterns."""
    
    def __init__(self, date_obj, nurse_fsf: str, nurse_sfs: str):
        # Convert to pandas Timestamp and normalize to remove time component
        if not isinstance(date_obj, pd.Timestamp):
            self.start_date = pd.Timestamp(date_obj).normalize()
        else:
            self.start_date = date_obj.normalize()
        self.nurse_fsf = nurse_fsf  # Nurse with First-Second-First pattern
        self.nurse_sfs = nurse_sfs  # Nurse with Second-First-Second pattern

    def to_dict(self) -> dict:
        """Convert assignment to dictionary representation."""
        return {
            "date": self.start_date.strftime('%Y-%m-%d'),  # Format as YYYY-MM-DD
            "nurse_fsf": self.nurse_fsf,
            "nurse_sfs": self.nurse_sfs
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'WeekendAssignment':
        """Create WeekendAssignment from dictionary representation."""
        return cls(
            pd.Timestamp(data["date"]),
            data["nurse_fsf"],
            data["nurse_sfs"]
        )


class NurseManager(DatabaseMixin):
    """Manages nurse data and availability with database persistence and caching."""
    
    # SQL queries as class constants
    LOAD_NURSES_QUERY = "SELECT nurse_id, name, is_prn, is_late_shift FROM nurses WHERE is_active=1"
    LOAD_UNAVAILABLE_DATES_QUERY = "SELECT nurse_id, date FROM unavailable_dates"
    GET_NURSE_UNAVAILABLE_DATES_QUERY = '''
        SELECT date FROM unavailable_dates 
        WHERE nurse_id=(SELECT nurse_id FROM nurses WHERE name=?)
    '''
    
    def __init__(self, db_name: str = 'nurse_schedule.db'):
        super().__init__(db_name)
        self._ensure_is_active_column()
        self._nurses = self._load_nurses()

    def _load_nurses(self) -> Dict[str, Dict[str, Any]]:
        """Load nurses and their unavailable dates from the database."""
        nurses_dict = {}
        
        # Load nurse basic information
        nurse_results = self.execute_query(self.LOAD_NURSES_QUERY)
        nurse_ids = {}
        
        for nurse_id, name, is_prn, is_late_shift in nurse_results:
            nurses_dict[name] = {
                "nurse_id": nurse_id,
                "is_prn": bool(is_prn),
                "is_late_shift": bool(is_late_shift),
                "unavailable_dates": set()
            }
            nurse_ids[nurse_id] = name

        # Load unavailable dates
        unavailable_results = self.execute_query(self.LOAD_UNAVAILABLE_DATES_QUERY)
        
        for nurse_id, date_str in unavailable_results:
            nurse_name = nurse_ids.get(nurse_id)
            if nurse_name:
                date_obj = DateUtils.safe_normalize_date(date_str)
                if date_obj:
                    nurses_dict[nurse_name]["unavailable_dates"].add(date_obj)
        
        return nurses_dict

    def refresh_cache(self) -> None:
        """Force reload of nurse data from database."""
        self._nurses = self._load_nurses()
        logger.info("Nurse manager cache refreshed")
    
    def reload(self) -> None:
        """
        Re-query the database and rebuild the internal nurse dictionary.
        Call this after any SQL that may have changed the nurses table.
        """
        self.refresh_cache()
    
    def get_non_prn_nurses(self) -> list[str]:
        """Get list of non-PRN nurses in a stable, deterministic order."""
        results = self.execute_query(
            'SELECT name FROM nurses WHERE is_prn=0 AND is_active=1 ORDER BY name COLLATE NOCASE'
        )
        return [row[0] for row in results]
    
    def get_prn_nurses(self) -> list[str]:
        """Get list of PRN nurses in a stable, deterministic order."""
        results = self.execute_query(
            'SELECT name FROM nurses WHERE is_prn=1 AND is_active=1 ORDER BY name COLLATE NOCASE'
        )
        return [row[0] for row in results]
    
    def get_nurses(self) -> list[str]:
        """Get all active nurses in a stable, deterministic order."""
        results = self.execute_query(
            'SELECT name FROM nurses WHERE is_active=1 ORDER BY name COLLATE NOCASE'
        )
        return [row[0] for row in results]
    
    
    @property
    def nurses(self) -> Dict[str, Dict[str, Any]]:
        """Get the nurses dictionary."""
        return self._nurses

    def is_prn_nurse(self, nurse: str) -> bool:
        """Check if a nurse is PRN (as needed)."""
        return self._nurses.get(nurse, {}).get("is_prn", False)

    def is_late_shift_nurse(self, nurse: str) -> bool:
        """Check if a nurse works late shifts."""
        return self._nurses.get(nurse, {}).get("is_late_shift", False)

    def get_unavailable_dates(self, nurse: str) -> Set[pd.Timestamp]:
        """Get a nurse's unavailable dates."""
        return self._nurses.get(nurse, {}).get("unavailable_dates", set())

    def add_nurse(self, name: str, is_prn: bool = False, is_late_shift: bool = False) -> None:
        """Add or reactivate a nurse. If the nurse exists, mark active and update flags."""
        # Insert if new; ignore if exists
        self.execute_update(
            'INSERT OR IGNORE INTO nurses (name, is_prn, is_late_shift) VALUES (?, ?, ?)',
            (name, is_prn, is_late_shift)
        )
        # Ensure active + sync flags (works for both new and existing rows)
        self.execute_update(
            'UPDATE nurses SET is_active=1, is_prn=?, is_late_shift=? WHERE name=?',
            (is_prn, is_late_shift, name)
        )
        self.refresh_cache()

#    def get_non_prn_nurses(self) -> List[str]:
#        """Get list of non-PRN nurses."""
#        results = self.execute_query('SELECT name FROM nurses WHERE is_prn=0 AND is_active=1')
#        return [row[0] for row in results]

#    def get_prn_nurses(self) -> List[str]:
#        """Get list of PRN nurses."""
#        results = self.execute_query('SELECT name FROM nurses WHERE is_prn=1 AND is_active=1')
#        return [row[0] for row in results]

    def remove_nurse(self, name: str, hard_delete: bool = False) -> None:
        """
        Deactivate a nurse by default (soft delete). If hard_delete=True,
        also clean up references and drop the row.
        """
        if hard_delete:
            self._hard_delete_nurse_and_cleanup(name)
            return
        # Soft-delete
        self.execute_update('UPDATE nurses SET is_active=0 WHERE name=?', (name,))
        self._nurses.pop(name, None)

#    def get_nurses(self) -> List[str]:
#        """Get all nurse names (active only)."""
#        results = self.execute_query('SELECT name FROM nurses WHERE is_active=1')
#        return [row[0] for row in results]

    def set_prn_status(self, name: str, is_prn: bool) -> None:
        """Update PRN status for a nurse."""
        self.execute_update('UPDATE nurses SET is_prn=? WHERE name=?', (is_prn, name))
        if name in self._nurses:
            self._nurses[name]["is_prn"] = is_prn

    def set_late_shift_status(self, name: str, is_late_shift: bool) -> None:
        """Update late shift status for a nurse."""
        self.execute_update('UPDATE nurses SET is_late_shift=? WHERE name=?', (is_late_shift, name))
        if name in self._nurses:
            self._nurses[name]["is_late_shift"] = is_late_shift

    def update_unavailable_dates(self, nurse_name: str, new_dates: Set) -> None:
        """Update unavailable dates for a nurse in the database and internal cache."""

        if not nurse_name:
            logger.warning("No nurse name provided for updating unavailable dates")
            return

        # Normalize incoming dates to midnight
        processed_dates: Set[pd.Timestamp] = set()
        for d in new_dates:
            d_obj = DateUtils.safe_normalize_date(d)
            if d_obj:
                processed_dates.add(d_obj)

        # Pull current dates from DB to compute deltas
        current_results = self.execute_query(self.GET_NURSE_UNAVAILABLE_DATES_QUERY, (nurse_name,))
        current_dates = set()
        for (date_str,) in current_results:
            d_obj = DateUtils.safe_normalize_date(date_str)
            if d_obj:
                current_dates.add(d_obj)

        dates_to_add = processed_dates - current_dates
        dates_to_remove = current_dates - processed_dates

        with self.get_db_connection() as conn:
            for date_obj in dates_to_add:
                conn.execute(
                    '''INSERT OR IGNORE INTO unavailable_dates (nurse_id, date)
                       VALUES ((SELECT nurse_id FROM nurses WHERE name=?), ?)''',
                    (nurse_name, date_obj.date().strftime('%Y-%m-%d'))
                )

            for date_obj in dates_to_remove:
                conn.execute(
                    '''DELETE FROM unavailable_dates
                       WHERE nurse_id=(SELECT nurse_id FROM nurses WHERE name=?) AND date=?''',
                    (nurse_name, date_obj.date().strftime('%Y-%m-%d'))
                )
            
            conn.commit()
        
        # Update cache
        if nurse_name in self._nurses:
            self._nurses[nurse_name]["unavailable_dates"] = processed_dates

    def get_prn_status(self, name: str) -> bool:
        """Get PRN status for a nurse."""
        return self.is_prn_nurse(name)

    def get_late_shift_status(self, name: str) -> bool:
        """Get late shift status for a nurse."""
        return self.is_late_shift_nurse(name)
    
    # --- New helpers for soft-delete / cleanup --------------------------------
    def activate_nurse(self, name: str) -> None:
        """Reactivate a previously deactivated nurse."""
        self.execute_update('UPDATE nurses SET is_active=1 WHERE name=?', (name,))
        self.refresh_cache()

    def deactivate_nurse(self, name: str) -> None:
        """Deactivate a nurse (soft delete)."""
        self.execute_update('UPDATE nurses SET is_active=0 WHERE name=?', (name,))
        self._nurses.pop(name, None)

    def _hard_delete_nurse_and_cleanup(self, name: str) -> None:
        """Hard-delete a nurse and clean up or null references in related tables."""
        with self.get_db_connection() as conn:
            cur = conn.execute('SELECT nurse_id FROM nurses WHERE name=?', (name,))
            row = cur.fetchone()
            if not row:
                return
            nurse_id = row[0]

            # Null out schedule_history references
            conn.execute("""
                UPDATE schedule_history
                SET main_nurse_id = CASE WHEN main_nurse_id = ? THEN NULL ELSE main_nurse_id END,
                    backup_nurse_id = CASE WHEN backup_nurse_id = ? THEN NULL ELSE backup_nurse_id END
            """, (nurse_id, nurse_id))

            # Null out weekend_assignments references (if table exists)
            conn.execute("""
                UPDATE weekend_assignments
                SET fsf_nurse_id = CASE WHEN fsf_nurse_id = ? THEN NULL ELSE fsf_nurse_id END,
                    sfs_nurse_id = CASE WHEN sfs_nurse_id = ? THEN NULL ELSE sfs_nurse_id END
            """, (nurse_id, nurse_id))

            # Remove availability + derived violation tables
            conn.execute("DELETE FROM unavailable_dates WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM rotation_violation_dates WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM rotation_violation_stats WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM weekend_rotation_history WHERE nurse_id = ?", (nurse_id,))

            # Finally remove nurse
            conn.execute("DELETE FROM nurses WHERE nurse_id = ?", (nurse_id,))
            conn.commit()
        self._nurses.pop(name, None)

    def cleanup_orphan_rows(self) -> None:
        """One-shot cleanup to reconcile older DBs with missing FKs."""
        with self.get_db_connection() as conn:
            conn.execute("""
                UPDATE schedule_history
                SET main_nurse_id = NULL
                WHERE main_nurse_id IS NOT NULL
                  AND main_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE schedule_history
                SET backup_nurse_id = NULL
                WHERE backup_nurse_id IS NOT NULL
                  AND backup_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE weekend_assignments
                SET fsf_nurse_id = NULL
                WHERE fsf_nurse_id IS NOT NULL
                  AND fsf_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE weekend_assignments
                SET sfs_nurse_id = NULL
                WHERE sfs_nurse_id IS NOT NULL
                  AND sfs_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("DELETE FROM unavailable_dates WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)")
            conn.execute("DELETE FROM rotation_violation_dates WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)")
            conn.execute("DELETE FROM rotation_violation_stats WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)")
            conn.execute("DELETE FROM weekend_rotation_history WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)")
            conn.commit()

    def _ensure_is_active_column(self) -> None:
        """Add is_active column to nurses if missing."""
        with self.get_db_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(nurses)").fetchall()]
            if 'is_active' not in cols:
                conn.execute("ALTER TABLE nurses ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
                conn.commit()


logger = logging.getLogger(__name__)

# Constants
class DBTables:
    WEEKEND_ASSIGNMENTS = "weekend_assignments"
    ROTATION_VIOLATION_DATES = "rotation_violation_dates"
    ROTATION_VIOLATION_STATS = "rotation_violation_stats"
    WEEKEND_ROTATION_HISTORY = "weekend_rotation_history"
    NURSES = "nurses"

class DBColumns:
    NURSE_ID = "nurse_id"
    NAME = "name"
    WEEKEND_START = "weekend_start"
    FSF_NURSE_ID = "fsf_nurse_id"
    SFS_NURSE_ID = "sfs_nurse_id"
    VIOLATION_DATE = "violation_date"
    PATTERN = "pattern"
    PREVIOUS_PATTERN = "previous_pattern"
    VIOLATION_COUNT = "violation_count"
    LAST_VIOLATION_DATE = "last_violation_date"
    CONSEC_VIOLATIONS = "consec_violations"
    LAST_PATTERN = "last_pattern"
    EXPECTED_NEXT_PATTERN = "expected_next_pattern"


class WeekendHistory:
    def __init__(self, db_name: str = "nurse_schedule.db"):
        self.db_name = db_name
        _ensure_violation_table(self.db_name)
        self._assignments = self._load_assignments()
        self._last_patterns = self._load_last_patterns()
        self.weekend_service = WeekendHistoryService(self)
        self.violation_service = ViolationHistoryService(self)

    # Utility Methods
    def _normalize_date(self, date_input) -> pd.Timestamp:
        """Normalize input date to pandas Timestamp at midnight."""
        return DateUtils.normalize_date(date_input)

    # Data Loading Methods
    def _load_assignments(self) -> Dict[pd.Timestamp, Tuple[Optional[str], Optional[str]]]:
        """Load weekend assignments from the database with normalized timestamps."""
        assignments = {}
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(f'''
                SELECT {DBColumns.WEEKEND_START}, nf.{DBColumns.NAME}, ns.{DBColumns.NAME}
                FROM {DBTables.WEEKEND_ASSIGNMENTS}
                LEFT JOIN {DBTables.NURSES} nf ON {DBColumns.FSF_NURSE_ID} = nf.{DBColumns.NURSE_ID}
                LEFT JOIN {DBTables.NURSES} ns ON {DBColumns.SFS_NURSE_ID} = ns.{DBColumns.NURSE_ID}
            ''')
            for weekend_start, fsf, sfs in cursor.fetchall():
                normalized_date = self._normalize_date(weekend_start)
                assignments[normalized_date] = (fsf, sfs)
        return assignments

    def _load_last_patterns(self) -> Dict[str, Optional[WeekendPattern]]:
        """Load last patterns for all nurses from database."""
        patterns: Dict[str, Optional[WeekendPattern]] = {}
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(f'''
                SELECT n.{DBColumns.NAME}, wrh.{DBColumns.LAST_PATTERN}
                FROM {DBTables.WEEKEND_ROTATION_HISTORY} wrh
                JOIN {DBTables.NURSES} n ON wrh.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            ''')
            for nurse, pattern_str in cursor.fetchall():
                patterns[nurse] = self._parse_weekend_pattern(pattern_str)
        return patterns

    def _parse_weekend_pattern(self, pattern_str: Optional[str]) -> Optional[WeekendPattern]:
        """Parse weekend pattern string safely."""
        if pattern_str is None:
            return None
        try:
            return WeekendPattern(pattern_str)
        except ValueError:
            return None

    # Violation Management Methods
    def get_violation_dates(self, nurse: str = None) -> list[tuple]:
        """Get violation dates for a nurse or all nurses."""
        with sqlite3.connect(self.db_name) as conn:
            if nurse:
                cursor = conn.execute(f"""
                    SELECT n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE}, 
                           vd.{DBColumns.PATTERN}, vd.{DBColumns.PREVIOUS_PATTERN}
                    FROM {DBTables.ROTATION_VIOLATION_DATES} vd
                    JOIN {DBTables.NURSES} n ON vd.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
                    WHERE n.{DBColumns.NAME} = ?
                    ORDER BY vd.{DBColumns.VIOLATION_DATE} ASC
                """, (nurse,))
            else:
                cursor = conn.execute(f"""
                    SELECT n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE}, 
                           vd.{DBColumns.PATTERN}, vd.{DBColumns.PREVIOUS_PATTERN}
                    FROM {DBTables.ROTATION_VIOLATION_DATES} vd
                    JOIN {DBTables.NURSES} n ON vd.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
                    ORDER BY n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE} ASC
                """)
            return cursor.fetchall()

    def _calculate_consecutive_violations(self, last_violation_date: Optional[pd.Timestamp],
                                        current_violation_date: pd.Timestamp,
                                        current_streak: int) -> int:
        """Calculate consecutive violation count."""
        if last_violation_date is None:
            return 1
        delta_days = (current_violation_date - last_violation_date).days
        return current_streak + 1 if delta_days == 7 else 1

    def _build_nurse_sequences(self) -> dict[str, list[tuple[pd.Timestamp, WeekendPattern]]]:
        """Build chronological sequences of assignments for each nurse."""
        return self._build_nurse_sequences_from_assignments(
            sorted(self._assignments.items())
        )

    def _build_nurse_sequences_from_assignments(
        self,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[Optional[str], Optional[str]]]],
    ) -> dict[str, list[tuple[pd.Timestamp, WeekendPattern]]]:
        """Build chronological sequences of assignments for each nurse."""
        seq_per_nurse: dict[str, list[tuple[pd.Timestamp, WeekendPattern]]] = {}

        for wk_start, (fsf, sfs) in chronological_assignments:
            if fsf:
                seq_per_nurse.setdefault(fsf, []).append((wk_start, WeekendPattern.FSF))
            if sfs:
                seq_per_nurse.setdefault(sfs, []).append((wk_start, WeekendPattern.SFS))

        return seq_per_nurse

    def _process_nurse_violations(self, nurse: str, sequence: list[tuple[pd.Timestamp, WeekendPattern]]) -> tuple[list, int, Optional[pd.Timestamp], int]:
        """Process violations for a single nurse's sequence."""
        violation_dates = []
        violation_count = 0
        last_violation_date = None
        streak = 0
        prev_violation_date = None

        for i in range(1, len(sequence)):
            prev_date, prev_pat = sequence[i-1]
            curr_date, curr_pat = sequence[i]

            if prev_pat == curr_pat:  # Violation detected
                violation_count += 1
                violation_dates.append((curr_date, curr_pat, prev_pat))
                last_violation_date = curr_date

                # Calculate consecutive streak
                if prev_violation_date is not None and (curr_date - prev_violation_date).days == 7:
                    streak += 1
                else:
                    streak = 1
                prev_violation_date = curr_date
            else:
                streak = 0

        return violation_dates, violation_count, last_violation_date, streak

    def _write_violation_dates_to_db(self, conn, nurse: str, violation_dates: list):
        """Write violation dates to database."""
        for vdate, vpat, vprev in violation_dates:
            date_str = vdate.strftime('%Y-%m-%d')
            conn.execute(f"""
                INSERT OR IGNORE INTO {DBTables.ROTATION_VIOLATION_DATES}
                ({DBColumns.NURSE_ID}, {DBColumns.VIOLATION_DATE},
                 {DBColumns.PATTERN}, {DBColumns.PREVIOUS_PATTERN})
                VALUES (
                    (SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME} = ?),
                    ?, ?, ?
                )
            """, (nurse, date_str, vpat.value, vprev.value))

    def _update_nurse_violation_stats(self, conn, nurse: str, violation_count: int, 
                                    last_violation_date: Optional[pd.Timestamp], streak: int):
        """Update violation stats for a nurse."""
        last_date_str = last_violation_date.strftime("%Y-%m-%d") if last_violation_date else None
        
        conn.execute(f"""
            INSERT INTO {DBTables.ROTATION_VIOLATION_STATS}
                  ({DBColumns.NURSE_ID}, {DBColumns.VIOLATION_COUNT}, 
                   {DBColumns.LAST_VIOLATION_DATE}, {DBColumns.CONSEC_VIOLATIONS})
            VALUES ((SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME}=?), ?, ?, ?)
            ON CONFLICT({DBColumns.NURSE_ID}) DO UPDATE
            SET {DBColumns.VIOLATION_COUNT} = excluded.{DBColumns.VIOLATION_COUNT},
                {DBColumns.LAST_VIOLATION_DATE} = excluded.{DBColumns.LAST_VIOLATION_DATE},
                {DBColumns.CONSEC_VIOLATIONS} = excluded.{DBColumns.CONSEC_VIOLATIONS}
        """, (nurse, violation_count, last_date_str, streak))

    def _rebuild_rotation_history(
        self,
        conn,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[Optional[str], Optional[str]]]],
    ) -> None:
        """Rebuild weekend rotation history table from chronological assignments."""
        conn.execute(f"DELETE FROM {DBTables.WEEKEND_ROTATION_HISTORY}")

        for _, (fsf, sfs) in chronological_assignments:
            if fsf:
                self._write_pattern(conn, fsf, WeekendPattern.FSF)
            if sfs:
                self._write_pattern(conn, sfs, WeekendPattern.SFS)

    def _rebuild_violation_tables(
        self,
        conn,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[Optional[str], Optional[str]]]],
    ) -> None:
        """Rebuild violation dates and stats from chronological assignments."""
        conn.execute(f"DELETE FROM {DBTables.ROTATION_VIOLATION_DATES}")
        conn.execute(f"DELETE FROM {DBTables.ROTATION_VIOLATION_STATS}")

        seq_per_nurse = self._build_nurse_sequences_from_assignments(chronological_assignments)

        for nurse, sequence in seq_per_nurse.items():
            (
                violation_dates,
                violation_count,
                last_violation_date,
                streak,
            ) = self._process_nurse_violations(nurse, sequence)

            self._write_violation_dates_to_db(conn, nurse, violation_dates)
            self._update_nurse_violation_stats(conn, nurse, violation_count, last_violation_date, streak)

    def _rebuild_last_patterns(self) -> None:
        """Reload in-memory last patterns from weekend rotation history table."""
        self._last_patterns = self._load_last_patterns()

    def _recalculate_violation_counts(self) -> None:
        """Rebuild violation tables from canonical assignments."""
        self.violation_service.rebuild()

    def _rebuild_derived_weekend_state(self) -> None:
        """Rebuild all derived weekend state from canonical weekend assignments."""
        self.weekend_service.rebuild()

    # Pattern Management Methods
    def _write_pattern(self, conn, nurse: str, new_pat: WeekendPattern) -> None:
        """Write pattern to database."""
        expected_next = WeekendPattern.FSF if new_pat == WeekendPattern.SFS else WeekendPattern.SFS
        
        conn.execute(f"""
            INSERT INTO {DBTables.WEEKEND_ROTATION_HISTORY} 
            ({DBColumns.NURSE_ID}, {DBColumns.LAST_PATTERN}, {DBColumns.EXPECTED_NEXT_PATTERN})
            VALUES (
                (SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME}=?),
                ?, ?
            )
            ON CONFLICT({DBColumns.NURSE_ID}) DO UPDATE
            SET {DBColumns.LAST_PATTERN}=excluded.{DBColumns.LAST_PATTERN},
                {DBColumns.EXPECTED_NEXT_PATTERN}=excluded.{DBColumns.EXPECTED_NEXT_PATTERN}
        """, (nurse, new_pat.value, expected_next.value))

    # Violation Statistics Methods
    def get_violation_summary(self, as_of=None):
        """Returns a DataFrame with violation summary for all nurses."""
        if as_of is None:
            as_of = pd.Timestamp.today().normalize()

        with sqlite3.connect(self.db_name) as conn:
            stats = pd.read_sql(f"""
                SELECT n.{DBColumns.NAME} as nurse,
                       COALESCE(vs.{DBColumns.VIOLATION_COUNT}, 0) as total_viol,
                       vs.{DBColumns.LAST_VIOLATION_DATE},
                       COALESCE(vs.{DBColumns.CONSEC_VIOLATIONS}, 0) as consec_viol
                FROM {DBTables.NURSES} n
                LEFT JOIN {DBTables.ROTATION_VIOLATION_STATS} vs
                  ON vs.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            """, conn, parse_dates=[DBColumns.LAST_VIOLATION_DATE])

        stats = stats.fillna({"total_viol": 0, DBColumns.LAST_VIOLATION_DATE: pd.NaT, "consec_viol": 0})

        def calculate_clean_run_weeks(row):
            """Calculate clean run weeks for a nurse."""
            last = row[DBColumns.LAST_VIOLATION_DATE]
            if pd.isna(last):
                return 999  # Never violated
            
            all_wks = self.get_weekends(row["nurse"])
            future = [wk for wk in all_wks if wk > last and wk <= as_of]
            
            viol_dates = {
                DateUtils.normalize_date(r[1])
                for r in self.get_violation_dates(row["nurse"])
            }
            
            clean_weeks = 0
            for wk in sorted(future):
                if wk not in viol_dates:
                    clean_weeks += 1
                else:
                    break
            return clean_weeks

        stats["clean_run_weeks"] = stats.apply(calculate_clean_run_weeks, axis=1).astype(int)
        stats["days_since_last"] = (
            as_of - stats[DBColumns.LAST_VIOLATION_DATE]
        ).dt.days.fillna(999).astype(int)
        
        return stats

    def get_violation_counts(self) -> dict[str, int]:
        """Get violation counts for all nurses."""
        with sqlite3.connect(self.db_name) as conn:
            cur = conn.execute(f"""
                SELECT n.{DBColumns.NAME}, COALESCE(vs.{DBColumns.VIOLATION_COUNT}, 0)
                FROM {DBTables.NURSES} n
                LEFT JOIN {DBTables.ROTATION_VIOLATION_STATS} vs
                       ON vs.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            """)
            return {name: cnt for name, cnt in cur.fetchall()}

    # Public Interface Methods
    def get_last_weekend_before(self, nurse: str, before_date: pd.Timestamp) -> Optional[pd.Timestamp]:
        """Get the last weekend assignment before a given date."""
        weekends = [w for w in self.get_weekends(nurse) if w < before_date]
        return max(weekends) if weekends else None

    def get_assignments(self) -> List[Tuple[pd.Timestamp, Optional[str], Optional[str]]]:
        """Get all assignments sorted by date."""
        return sorted(
            [(date, fsf, sfs) for date, (fsf, sfs) in self._assignments.items()],
            key=lambda x: x[0]
        )

    def get_last_pattern(self, nurse: str) -> Optional[WeekendPattern]:
        """Get the last pattern for a nurse."""
        return self._last_patterns.get(nurse)

    def get_weekends(self, nurse: str) -> List[pd.Timestamp]:
        """Get all weekend assignments for a nurse."""
        return [
            weekend_start for weekend_start, (fsf, sfs) in self._assignments.items()
            if nurse in (fsf, sfs)
        ]

    def backup(self) -> List[Tuple[pd.Timestamp, Optional[str], Optional[str]]]:
        """Create a backup of all assignments."""
        return copy.deepcopy(self.get_assignments())

    def restore(self, backup_assignments: list):
        """Restore assignments from backup."""
        self.weekend_service.restore_assignments(backup_assignments)

    def set_violation_count(self, nurse: str, count: int) -> None:
        """Manual override for a nurse's violation count.

        Persists until the next canonical-from-assignments rebuild.
        """
        self.violation_service.set_violation_count(nurse, count)

    def set_last_pattern(self, nurse: str, pattern: WeekendPattern) -> None:
        """Manual override for a nurse's last weekend pattern.

        Persists until the next canonical-from-assignments rebuild.
        """
        self.weekend_service.set_last_pattern(nurse, pattern)

    def add_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str):
        """Add a new weekend assignment."""
        self.weekend_service.add_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def modify_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str):
        """Modify an existing weekend assignment."""
        self.weekend_service.modify_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def remove_assignment(self, weekend_start):
        """Remove a weekend assignment."""
        self.weekend_service.remove_assignment(weekend_start)
        
class PreScheduler:
    def __init__(self, db_name='nurse_schedule.db'):
        self.db_name = db_name
        self._assignments = self._load_assignments()

    @staticmethod
    def _normalize_date(date_input) -> pd.Timestamp:
        """
        Convert *anything* (str / date / datetime / Timestamp) to a
        pandas.Timestamp normalised to 00:00.
        """
        return DateUtils.normalize_date(date_input)
        
    
    def _load_assignments(self) -> dict[pd.Timestamp, dict]:
        """
        Read the table once and keep it in memory.
        The dictionary key is now a **normalised pandas.Timestamp** so
        all later comparisons use the exact same representation.
        """
        assignments: dict[pd.Timestamp, dict] = {}

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(
                """
                SELECT psa.date, nm.name, nb.name
                FROM   pre_scheduled_assignments psa
                LEFT   JOIN nurses nm ON psa.main_nurse_id  = nm.nurse_id
                LEFT   JOIN nurses nb ON psa.backup_nurse_id = nb.nurse_id
                """
            )
            for date_str, main, backup in cursor.fetchall():
                ts = self._normalize_date(date_str)
                assignments[ts] = {"main": main, "backup": backup}

        return assignments

    def get_assignments_in_range(
        self,
        start_date,
        end_date
    ) -> dict[pd.Timestamp, dict[str, Optional[str]]]:
        """
        Inclusive filter on the in-memory dictionary.  The two boundary
        arguments can be str / datetime / Timestamp.
        """
        start = self._normalize_date(start_date)
        end   = self._normalize_date(end_date)

        return {
            ts: info
            for ts, info in self._assignments.items()
            if start <= ts <= end
        }
        
    def add_assignment(self, date_str, main_nurse, backup_nurse, note=""):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO pre_scheduled_assignments (date, main_nurse_id, backup_nurse_id, note)
                VALUES (?, 
                        (SELECT nurse_id FROM nurses WHERE name=?),
                        (SELECT nurse_id FROM nurses WHERE name=?),
                        ?)
            ''', (date_str, main_nurse, backup_nurse, note))
        self._assignments=self._load_assignments()
        
    def remove_assignment(self, date_str):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('DELETE FROM pre_scheduled_assignments WHERE date=?', (date_str,))
        self._assignments=self._load_assignments()
        
    def get_assignments(self):
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute('''
                SELECT date, nm.name, nb.name, note
                FROM pre_scheduled_assignments
                LEFT JOIN nurses nm ON main_nurse_id=nm.nurse_id
                LEFT JOIN nurses nb ON backup_nurse_id=nb.nurse_id
            ''')
            return cursor.fetchall()


class Role(str, Enum):
    """Nurse assignment roles."""
    MAIN = "main"
    BACKUP = "backup"


class NurseManagerProtocol(Protocol):
    """Protocol defining the nurse manager interface."""
    db_name: str
    nurses: Dict[str, Dict[str, Any]]
    
    def is_prn_nurse(self, nurse: str) -> bool: ...
    def is_late_shift_nurse(self, nurse: str) -> bool: ...


class WeekendHistoryProtocol(Protocol):
    """Protocol defining the weekend history interface."""
    def get_last_pattern(self, nurse: str) -> Optional[str]: ...
    def get_weekends(self, nurse: str) -> List[pd.Timestamp]: ...


class PreSchedulerProtocol(Protocol):
    def get_assignments_in_range(
        self, start_date: Union[str, datetime.date, pd.Timestamp],
        end_date:   Union[str, datetime.date, pd.Timestamp]
    ) -> Dict[pd.Timestamp, Dict[str, Optional[str]]]: ...
    

class SchedulerConfig:
    def __init__(self,
                 weekend_gap_days: int = 14,
                 main_score_factor: int = 10,
                 backup_score_factor: int = 10,
                 availability_penalty: int = 10,
                 min_days_between_assignments: int = 2,
                 allow_post_weekend_wednesday_main: bool = False,
                 allow_post_weekend_wednesday_backup: bool = True,
                 allow_post_weekend_thursday_main: bool = True,
                 allow_post_weekend_thursday_backup: bool = True,
                 *,
                 scoring_weights: dict[str, float] | None = None,
                 # NEW:
                 allow_one_day_weekday_gap: bool = False,
                 **extra):
 
        if allow_one_day_weekday_gap is None:
            allow_one_day_weekday_gap = False
        else:
            allow_one_day_weekday_gap = bool(allow_one_day_weekday_gap)

        self.allow_one_day_weekday_gap               = allow_one_day_weekday_gap
        self.weekend_gap_days                          = weekend_gap_days
        self.main_score_factor                         = main_score_factor
        self.backup_score_factor                       = backup_score_factor
        self.availability_penalty                      = availability_penalty
        self.min_days_between_assignments              = min_days_between_assignments
        self.allow_post_weekend_wednesday_main         = allow_post_weekend_wednesday_main
        self.allow_post_weekend_wednesday_backup       = allow_post_weekend_wednesday_backup
        self.allow_post_weekend_thursday_main          = allow_post_weekend_thursday_main
        self.allow_post_weekend_thursday_backup        = allow_post_weekend_thursday_backup

        # ── per-metric weights for the composite schedule score ───
        default_weights = {
            "rotation_rep" : 0.30,   # pattern repeat count
            "gaps"         : 0.20,   # weekday gap-fill penalty
            "rot_viol"     : 0.15,   # historic rotation violations
            "weekend_gap"  : 0.15,   # fairness of “time-since-last-wknd”
            "balance"      : 0.10,   # main/backup daily balance
            "long_term"    : 0.10,   # 30-day over/under utilisation
        }

        if scoring_weights is None:
            self.scoring_weights = default_weights.copy()
        else:
            merged = default_weights.copy()
            for k, v in scoring_weights.items():
                if k in merged:
                    merged[k] = max(0.0, float(v))         # clip negatives
            self.scoring_weights = merged

        # normalise so Σw == 1 (absolute scale irrelevant)
        total = sum(self.scoring_weights.values()) or 1.0
        for k in self.scoring_weights:
            self.scoring_weights[k] /= total

        # ── keep any additional keyword untouched (future proofing) ────
        for k, v in extra.items():
            setattr(self, k, v)
            

class WeekBackup(NamedTuple):
    """
    Immutable snapshot of the four objects a week-permutation may have
    to roll back to.
    """
    rows:            pd.DataFrame      # schedule slice - columns ["main","backup"]
    main_counts:     pd.Series         # copy of main_assignment_counts
    backup_counts:   pd.Series         # copy of backup_assignment_counts
    last_assignment: dict              # deepcopy of last_assignment



class Comparison(Enum):
    """Trinary comparison helpers used by the tracker."""

    BETTER = -1
    EQUAL = 0
    WORSE = 1


@dataclass(frozen=True)
class ScheduleQuality:
    """Unified quality metric capturing the primary optimisation goals."""

    total_gaps: int
    backup_spread: int
    main_spread: int
    total_spread: int
    rotation_penalty: int
    weekend_penalty: float
    history_penalty: float
    weighted_score: float = 0.0

    @classmethod
    def from_variant(
        cls,
        variant: "ScheduleVariant",
        scheduler: Optional["NurseScheduler"] = None,
    ) -> "ScheduleQuality":
        """Calculate the quality metrics from a schedule variant.

        NOTE:
        - ``compare_to`` uses these fields lexicographically for local-search
          accept/revert decisions in ``BestStateTracker``.
        - ``weighted_score`` is retained for reporting/debugging only; final
          candidate ranking is done by ``NurseScheduler._score_and_rank_variants``
          after metric normalization and weighting.
        """

        weekend_penalty = 0.0
        if scheduler and hasattr(scheduler, "_weekend_gap_penalty"):
            try:
                weekend_penalty = float(
                    scheduler._weekend_gap_penalty(variant.state.schedule)
                )
            except Exception:  # pragma: no cover - defensive guard
                weekend_penalty = 0.0

        history_penalty = 0.0
        if (
            scheduler
            and hasattr(scheduler, "_long_term_score")
            and hasattr(scheduler, "_historic_overage")
        ):
            try:
                nurse_counts: dict[str, dict[str, int]] = {}
                for nurse in variant.state.main_assignment_counts.index:
                    m = int(variant.state.main_assignment_counts.get(nurse, 0))
                    b = int(variant.state.backup_assignment_counts.get(nurse, 0))
                    nurse_counts[str(nurse)] = {
                        "main": m,
                        "backup": b,
                        "total": m + b,
                    }

                overage = scheduler._historic_overage()
                history_penalty = float(
                    scheduler._long_term_score(nurse_counts, overage)
                )
            except Exception:  # pragma: no cover - defensive guard
                history_penalty = 0.0

        weighted_score = variant._rebalance_score(alpha=1.0, beta=1.0)
        metrics = compute_quality_metrics(
            schedule_df=variant.state.schedule,
            main_counts=variant.state.main_assignment_counts,
            backup_counts=variant.state.backup_assignment_counts,
            rotation_repeats=int(getattr(variant.state, "rotation_repeats", 0)),
            count_gaps_fn=lambda df: variant.count_gaps(),
            weekend_penalty=weekend_penalty,
            history_penalty=history_penalty,
            weighted_score=weighted_score,
        )

        return cls(
            total_gaps=metrics.total_gaps,
            backup_spread=metrics.backup_spread,
            main_spread=metrics.main_spread,
            total_spread=metrics.total_spread,
            rotation_penalty=metrics.rotation_penalty,
            weekend_penalty=metrics.weekend_penalty,
            history_penalty=metrics.history_penalty,
            weighted_score=metrics.weighted_score,
        )

    def compare_to(self, other: "ScheduleQuality", *, float_tol: float = 1e-6) -> Comparison:
        """Lexicographic comparison with tolerance for float metrics.

        This order is the tracker acceptance policy (earlier keys dominate):
        gaps -> spreads -> rotation -> weekend-gap penalty -> long-term history.
        """

        mine = QualityMetrics(
            total_gaps=self.total_gaps,
            backup_spread=self.backup_spread,
            main_spread=self.main_spread,
            total_spread=self.total_spread,
            rotation_penalty=self.rotation_penalty,
            weekend_penalty=self.weekend_penalty,
            history_penalty=self.history_penalty,
            weighted_score=self.weighted_score,
        )
        theirs = QualityMetrics(
            total_gaps=other.total_gaps,
            backup_spread=other.backup_spread,
            main_spread=other.main_spread,
            total_spread=other.total_spread,
            rotation_penalty=other.rotation_penalty,
            weekend_penalty=other.weekend_penalty,
            history_penalty=other.history_penalty,
            weighted_score=other.weighted_score,
        )
        result = compare_quality(mine, theirs, float_tol=float_tol)
        if result == QualityComparison.BETTER:
            return Comparison.BETTER
        if result == QualityComparison.WORSE:
            return Comparison.WORSE
        return Comparison.EQUAL

    def is_better_than(self, other: "ScheduleQuality") -> bool:
        return self.compare_to(other) == Comparison.BETTER

    def is_at_least_as_good(self, other: "ScheduleQuality") -> bool:
        return self.compare_to(other) in (Comparison.BETTER, Comparison.EQUAL)

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return (
            "Quality("
            f"gaps={self.total_gaps}, "
            f"spread=({self.backup_spread},{self.main_spread},{self.total_spread}), "
            f"rot={self.rotation_penalty}, "
            f"wknd={self.weekend_penalty:.3f}, "
            f"hist={self.history_penalty:.3f})"
        )


@dataclass
class StateSnapshot:
    """Complete snapshot of schedule state with index pinning."""

    quality: ScheduleQuality
    schedule_rows: pd.DataFrame
    schedule_index: pd.Index
    main_counts: pd.Series
    backup_counts: pd.Series
    last_assignment: dict
    rotation_repeats: int
    weekend_tracking: dict
    nurse_weekend_lists: dict
    last_pattern: dict
    index_hash: int

    @property
    def schedule(self) -> pd.DataFrame:
        """Backward-compatible alias for tests/debug code expecting ``snapshot.schedule``."""
        return self.schedule_rows.copy()

    @classmethod
    def capture(
        cls,
        variant: "ScheduleVariant",
        quality: ScheduleQuality,
    ) -> "StateSnapshot":
        sched = variant.state.schedule
        idx = sched.index.copy()

        return cls(
            quality=quality,
            schedule_rows=sched[["main", "backup"]].copy(),
            schedule_index=idx,
            main_counts=variant.state.main_assignment_counts.copy(),
            backup_counts=variant.state.backup_assignment_counts.copy(),
            last_assignment=dict(variant.state.last_assignment),
            rotation_repeats=variant.state.rotation_repeats,
            weekend_tracking=dict(variant.state.weekend_tracking),
            nurse_weekend_lists={k: list(v) for k, v in variant.state.nurse_weekend_lists.items()},
            last_pattern=dict(variant.state.last_pattern),
            index_hash=hash(tuple(idx)),
        )

    def restore_to(self, variant: "ScheduleVariant") -> None:
        current_idx = variant.state.schedule.index
        current_hash = hash(tuple(current_idx))

        if current_hash != self.index_hash:
            raise ValueError(
                "Index mismatch: schedule index changed between snapshot and restore. "
                f"Expected hash {self.index_hash}, got {current_hash}"
            )

        variant.state.schedule.loc[
            self.schedule_index, ["main", "backup"]
        ] = self.schedule_rows

        variant.state.main_assignment_counts = self.main_counts.copy()
        variant.state.backup_assignment_counts = self.backup_counts.copy()
        variant.state.last_assignment = dict(self.last_assignment)
        variant.state.rotation_repeats = self.rotation_repeats
        variant.state.weekend_tracking = dict(self.weekend_tracking)
        variant.state.nurse_weekend_lists = {k: list(v) for k, v in self.nurse_weekend_lists.items()}
        variant.state.last_pattern = dict(self.last_pattern)

        variant._invalidate_weekday_cache()


class BestStateTracker:
    """Manage best-state tracking and plateau-aware navigation."""

    def __init__(
        self,
        variant: "ScheduleVariant",
        scheduler: Optional["NurseScheduler"] = None,
    ) -> None:
        self.variant = variant
        self.scheduler = scheduler

        self._global_best: Optional[StateSnapshot] = None
        self._global_best_quality: Optional[ScheduleQuality] = None

        self._iteration_snapshot: Optional[StateSnapshot] = None
        self._iteration_quality: Optional[ScheduleQuality] = None

        self._plateau_depth = 0
        self._max_plateau_depth = 10

        self._improvements = 0
        self._neutrals = 0
        self._reversions = 0

    def initialize(self) -> ScheduleQuality:
        self.variant._recalculate_assignment_counts()
        self.variant._update_last_assignment_dates()

        quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        snapshot = StateSnapshot.capture(self.variant, quality)

        self._global_best = snapshot
        self._global_best_quality = quality

        print(f"[Tracker] Initialized with: {quality}")
        return quality

    def begin_iteration(self, phase_name: str = "") -> ScheduleQuality:
        # Counts are maintained incrementally by _inc_assign/_dec_assign and
        # restored exactly by StateSnapshot.restore_to(); skip expensive
        # full-schedule recalculation.
        quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        snapshot = StateSnapshot.capture(self.variant, quality)

        self._iteration_snapshot = snapshot
        self._iteration_quality = quality

        if phase_name:
            print(f"[Tracker] {phase_name}: Begin iteration with {quality}")

        return quality

    def evaluate_and_commit(
        self,
        *,
        phase_name: str = "",
        allow_neutral: bool = False,
    ) -> Comparison:
        if self._iteration_quality is None or self._iteration_snapshot is None:
            raise RuntimeError("Must call begin_iteration() before evaluate_and_commit().")

        # Counts are maintained incrementally; skip full recalculation.
        current_quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        # Local-search acceptance is *lexicographic* via ScheduleQuality.compare_to.
        # This is intentionally deterministic and stricter than the final
        # cross-variant weighted ranking used after search completes.
        comparison = current_quality.compare_to(self._iteration_quality)

        if comparison == Comparison.BETTER:
            self._improvements += 1
            self._plateau_depth = 0

            if (
                self._global_best_quality is None
                or current_quality.is_better_than(self._global_best_quality)
            ):
                self._global_best = StateSnapshot.capture(self.variant, current_quality)
                self._global_best_quality = current_quality
                if phase_name:
                    print(f"[Tracker] {phase_name}: NEW GLOBAL BEST: {current_quality}")
            else:
                if phase_name:
                    print(
                        f"[Tracker] {phase_name}: Local improvement: "
                        f"{self._iteration_quality} -> {current_quality}"
                    )

            return Comparison.BETTER

        if comparison == Comparison.EQUAL and allow_neutral:
            if self._plateau_depth < self._max_plateau_depth:
                self._neutrals += 1
                self._plateau_depth += 1
                if phase_name:
                    print(
                        f"[Tracker] {phase_name}: Neutral move accepted "
                        f"(plateau={self._plateau_depth}): {current_quality}"
                    )
                return Comparison.EQUAL

            if phase_name:
                print(f"[Tracker] {phase_name}: Plateau limit reached, reverting")
            self._revert_to_iteration()
            return Comparison.WORSE

        self._reversions += 1
        if phase_name:
            print(f"[Tracker] {phase_name}: No improvement, reverting: {current_quality}")
        self._revert_to_iteration()
        return Comparison.WORSE

    def _revert_to_iteration(self) -> None:
        if self._iteration_snapshot is None:
            raise RuntimeError("No iteration snapshot to revert to.")

        self._iteration_snapshot.restore_to(self.variant)

    def restore_global_best(self) -> None:
        if self._global_best is None:
            print("[Tracker] Warning: No global best to restore")
            return

        print(f"[Tracker] Restoring global best: {self._global_best_quality}")
        self._global_best.restore_to(self.variant)

    def get_global_best_quality(self) -> Optional[ScheduleQuality]:
        return self._global_best_quality

    def get_statistics(self) -> dict:
        return {
            "improvements": self._improvements,
            "neutrals": self._neutrals,
            "reversions": self._reversions,
            "plateau_depth": self._plateau_depth,
            "global_best": str(self._global_best_quality)
            if self._global_best_quality
            else None,
        }

    def reset_plateau(self) -> None:
        self._plateau_depth = 0



# Constants for better readability
DAYS_IN_WEEKEND = 3
FRIDAY_WEEKDAY = 4
MONDAY_WEEKDAY = 0
TUESDAY_WEEKDAY = 1
WEDNESDAY_WEEKDAY = 2
THURSDAY_WEEKDAY = 3
DEFAULT_PRE_WEEKEND_WINDOW = 4
DEFAULT_POST_WEEKEND_WINDOW = 6
MAX_MAIN_ASSIGNMENTS_PER_WEEK = 1
MAX_TOTAL_ASSIGNMENTS_PER_WEEK = 2


class ScheduleState:
    """
    Immutable snapshot that can be cloned for every search-tree branch.
    Deep-copies guarantee that no mutable object is ever shared across
    ScheduleVariants.
    """
    
    def __init__(
        self,
        schedule: pd.DataFrame,
        main_assignment_counts: pd.Series,
        backup_assignment_counts: pd.Series,
        last_assignment: Dict,
        last_pattern: Dict,
        weekend_tracking: Dict,
        nurse_weekend_lists: Optional[Dict] = None,
        rotation_repeats: int = 0,
    ):
        # Always own *private* copies of mutable objects
        self.schedule = schedule.copy()
        self.main_assignment_counts = main_assignment_counts.copy()
        self.backup_assignment_counts = backup_assignment_counts.copy()
        self.last_assignment = dict(last_assignment)
        self.last_pattern = dict(last_pattern)
        self.weekend_tracking = dict(weekend_tracking)

        # Per-nurse Friday lists
        if nurse_weekend_lists is None:
            self.nurse_weekend_lists = {
                n: [] for n in self.main_assignment_counts.index
            }
        else:
            self.nurse_weekend_lists = {k: list(v) for k, v in nurse_weekend_lists.items()}

        # Rotation-repeat counter used for variant ranking
        self.rotation_repeats = rotation_repeats

    def clone(self) -> "ScheduleState":
        """
        Produce a fully detached copy. Every mutable member is either
        .copy() (for pandas objects) or deepcopy() (for plain Python
        containers), so no ScheduleVariant can mutate another one's data.
        """
        return ScheduleState(
            self.schedule.copy(),
            self.main_assignment_counts.copy(),
            self.backup_assignment_counts.copy(),
            dict(self.last_assignment),
            dict(self.last_pattern),
            dict(self.weekend_tracking),
            nurse_weekend_lists={k: list(v) for k, v in self.nurse_weekend_lists.items()},
            rotation_repeats=self.rotation_repeats,
        )


class ScheduleVariant:
    """
    One branch of the search tree with 100% isolation: every mutable object 
    is privately owned, so no data leak can occur between variants.
    """
    
    def __init__(
        self,
        state: ScheduleState,
        nurses: List[str],
        availability: pd.DataFrame,
        config: 'SchedulerConfig',
        nurse_manager: 'NurseManagerProtocol',
        pre_scheduled: Optional[Dict] = None,
        historical_main: Optional[Dict] = None,
        historical_backup: Optional[Dict] = None,
        console_debug: Optional[bool] = None,
        _skip_copy: bool = False,
    ):
        # All *mutable* arguments are copied so the caller keeps ownership
        # _skip_copy=True is used by clone() to avoid redundant copies of read-only data
        self.state = state
        self.nurses = nurses if _skip_copy else nurses[:]
        self.availability = availability if _skip_copy else availability.copy()
        self.config = config
        self.nurse_manager = nurse_manager

        self._order_index = {n: i for i, n in enumerate(self.nurses)}
        self._weekday_counts_cache: dict[int, dict[str, int]] = {}
        self._total_counts_cache = None
        self._index_set = None

        # 30-day history used only for tie-breaking (read-only)
        if _skip_copy:
            self.hist_main = historical_main or {}
            self.hist_backup = historical_backup or {}
        else:
            self.hist_main = (historical_main or {}).copy()
            self.hist_backup = (historical_backup or {}).copy()

        # Cache of late-shift staff
        self._late_set = {
            n for n in self.nurses if nurse_manager.is_late_shift_nurse(n)
        }
        
        # Immutable dict supplied by caller – we may share safely
        self.pre_scheduled = pre_scheduled or {}

        if console_debug is None:
            env_val = os.getenv("SCHEDULE_VARIANT_DEBUG", "1")
            try:
                self._console_debug = bool(int(env_val))
            except ValueError:
                self._console_debug = env_val.lower() in {"1", "true", "yes", "on"}
        else:
            self._console_debug = bool(console_debug)

        self.assignment_debug_logger = ASSIGNMENT_DEBUG_LOGGER
        self.Comparison = Comparison
        self.StateSnapshot = StateSnapshot
        self.BestStateTracker = BestStateTracker
        self.DEFAULT_POST_WEEKEND_WINDOW = DEFAULT_POST_WEEKEND_WINDOW
        self.DEFAULT_PRE_WEEKEND_WINDOW = DEFAULT_PRE_WEEKEND_WINDOW
        self.pd = pd
        # NOTE: search helpers (domain_builder, order_generator, window_optimizer)
        # are constructed lazily via @cached_property below so they only hold a
        # reference to this variant through the VariantSearchContext surface,
        # not via an unconditional back-reference at __init__ time.

        self._initialize_pre_scheduled_slots()

    def _debug_print(self, msg: str, **kwargs) -> None:
        if self._console_debug:
            print(msg, **kwargs, flush=True)

    @staticmethod
    def is_empty(value) -> bool:
        return is_empty(value)

    # ------------------------------------------------------------------
    # VariantSearchContext public surface
    # ------------------------------------------------------------------
    # The optimizer/builder/ordering helpers depend on this stable public
    # surface (see scheduler/generation/context.py). Underscored aliases
    # are retained as internal call sites — they may be removed once all
    # internal references to the underscored names are migrated.

    @property
    def console_debug(self) -> bool:
        return self._console_debug

    @property
    def order_index(self) -> dict[str, int]:
        return self._order_index

    def debug_print(self, msg: str, **kwargs) -> None:
        return self._debug_print(msg, **kwargs)

    def is_pre_scheduled(self, date: pd.Timestamp, role: str) -> bool:
        return self._is_pre_scheduled(date, role)

    def eligible_domain(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self._eligible_domain(date, role, diagnostics, force_relaxed=force_relaxed)

    def eligible_domain_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self._eligible_domain_gap(date, role, diagnostics, force_relaxed=force_relaxed)

    def get_eligible_nurses_for_day(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        return self._get_eligible_nurses_for_day(
            date, role, diagnostics, relaxed_spacing=relaxed_spacing
        )

    def get_eligible_nurses_for_day_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        return self._get_eligible_nurses_for_day_gap(
            date, role, diagnostics, relaxed_spacing=relaxed_spacing
        )

    def inc_assign(
        self,
        date: pd.Timestamp,
        role: str,
        nurse: str,
        *,
        gap_phase: bool = False,
    ) -> bool:
        return self._inc_assign(date, role, nurse, gap_phase=gap_phase)

    def dec_assign(self, date: pd.Timestamp, role: str, nurse: str) -> None:
        return self._dec_assign(date, role, nurse)

    def spread_components(self) -> tuple[int, int, int]:
        return self._spread_components()

    def lexi_better(
        self,
        new_tuple: tuple[int, int, int],
        base_tuple: tuple[int, int, int],
    ) -> bool:
        return self._lexi_better(new_tuple, base_tuple)

    def collect_weekday_windows(self, window_weeks: int = 2) -> list[list[pd.Timestamp]]:
        return self._collect_weekday_windows(window_weeks=window_weeks)

    def build_window_varlist(
        self, days: list[pd.Timestamp]
    ) -> list[tuple[pd.Timestamp, str]]:
        return self._build_window_varlist(days)

    def clear_window_assignments(self, days: list[pd.Timestamp]) -> None:
        return self._clear_window_assignments(days)

    def restore_from_backup(self, days: list[pd.Timestamp], backup) -> None:
        return self._restore_from_backup(days, backup)

    def get_all_weekdays(self) -> list[pd.Timestamp]:
        return self._get_all_weekdays()

    def gen_full_orders(
        self,
        days: list[pd.Timestamp],
        max_orders: int = 50,
    ) -> list[list[tuple[pd.Timestamp, str]]]:
        return self._gen_full_orders(days, max_orders=max_orders)

    def backtrack_full_order(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
    ) -> bool:
        return self._backtrack_full_order(vars_list, deadline, node_budget)

    def recalculate_assignment_counts(self) -> None:
        return self._recalculate_assignment_counts()

    def update_last_assignment_dates(self) -> None:
        return self._update_last_assignment_dates()

    def get_total_counts(self) -> pd.Series:
        return self._get_total_counts()

    def weekday_counts_for(self, weekday: int) -> dict[str, int]:
        return self._weekday_counts_for(weekday)

    def log_assignment_debug(self, **kwargs) -> None:
        return self._log_assignment_debug(**kwargs)

    # Search helpers — lazily constructed against the public context surface
    # rather than being held as eager back-references. This means a freshly
    # constructed variant does not unconditionally instantiate these helpers,
    # and helpers depend only on the `VariantSearchContext` protocol.
    @cached_property
    def domain_builder(self) -> "CandidateDomainBuilder":
        return CandidateDomainBuilder(self)

    @cached_property
    def order_generator(self) -> "OrderGenerator":
        return OrderGenerator(self)

    @cached_property
    def window_optimizer(self) -> "WindowRefillOptimizer":
        return WindowRefillOptimizer(self)

    def _initialize_pre_scheduled_slots(self) -> None:
        """Initialize schedule with pre-scheduled assignments and update counters."""
        sched = self.state.schedule
        for day, slot in self.pre_scheduled.items():
            for role, nurse in slot.items():
                if nurse and is_empty(sched.at[day, role]):
                    sched.at[day, role] = nurse

        self._invalidate_weekday_cache()
        # Ensure counters & last-assignment dictionaries reflect the seeding
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()
    
        # After placing pre-scheduled cells, ensure our per-nurse Friday lists
        # include any weekend already seeded (Fri/Sat/Sun).
        for friday in [d for d in sched.index if d.weekday() == FRIDAY_WEEKDAY]:
            weekend_days = [
                friday,
                friday + timedelta(days=1),
                friday + timedelta(days=2),
            ]
            for wd in weekend_days:
                if wd not in sched.index:
                    continue
                for role in ("main", "backup"):
                    n = sched.at[wd, role]
                    if n is None or is_empty(n):
                        continue
                    lst = self.state.nurse_weekend_lists.setdefault(n, [])
                    # Insert friday once per nurse per weekend (avoid duplicates)
                    pos = bisect.bisect_left(lst, friday)
                    if pos >= len(lst) or lst[pos] != friday:
                        bisect.insort(lst, friday)
    
    def clone(self) -> "ScheduleVariant":
        """
        Return a *completely* detached copy of this variant with independent
        copies of all mutable state.  Read-only data (availability, hist,
        nurses, config, nurse_manager) is shared via _skip_copy=True.
        """
        return ScheduleVariant(
            state=self.state.clone(),
            nurses=self.nurses,
            availability=self.availability,
            config=self.config,
            nurse_manager=self.nurse_manager,
            pre_scheduled=self.pre_scheduled,
            historical_main=self.hist_main,
            historical_backup=self.hist_backup,
            console_debug=self._console_debug,
            _skip_copy=True,
        )
   
    def _is_pre_scheduled(self, date: pd.Timestamp, role: str) -> bool:
        """Returns True if the given date/role is pre-scheduled with a non-empty nurse name."""
        slot = self.pre_scheduled.get(date)
        nurse = slot.get(role) if slot else None
        return nurse is not None and str(nurse).strip() != ""

    def _modify_schedule(
        self,
        date_range: Iterable[pd.Timestamp],
        value: Optional[str] = None
    ) -> None:
        """Modify schedule for given date range, skipping pre-scheduled cells."""
        sched = self.state.schedule
        for d in date_range:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                sched.at[d, role] = value
        self._invalidate_weekday_cache()
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    # ===== CONFLICT DETECTION =====
    
    def _late_shift_conflict(self, nurse: str, date: pd.Timestamp, role: str) -> bool:
        """Check if assigning this nurse would create a late-shift conflict."""
        if nurse not in self._late_set:
            return False
        other_role = "backup" if role == "main" else "main"
        other_nurse = self.state.schedule.at[date, other_role]
        if other_nurse not in self._late_set:
            return False
        if self._is_pre_scheduled(date, role) and self._is_pre_scheduled(date, other_role):
            return False  # allow pre-scheduled late/late combinations
        return True

    # ===== WEEKEND ASSIGNMENT =====
    
    def assign_weekend(
        self,
        weekend_start: pd.Timestamp,
        fsf_nurse: str,
        sfs_nurse: str,
    ) -> None:
        """
        Write FSF/SFS pattern into schedule and update all counters.
        Counts every individual pattern repeat instead of lumping repeats together.
        """
        weekend_dates = self._get_weekend_dates(weekend_start)
        assignments = self._get_weekend_assignments(fsf_nurse, sfs_nurse)

        self._apply_weekend_assignments(weekend_dates, assignments)
        self._update_weekend_tracking(weekend_start, fsf_nurse, sfs_nurse)
        self._update_nurse_weekend_lists(fsf_nurse, sfs_nurse, weekend_start)
        self._update_pattern_tracking(fsf_nurse, sfs_nurse)
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    def _weekday_relaxation_applicable(self, nurse: str, date: pd.Timestamp) -> bool:
        """
        Relaxed (one-day) spacing is only allowed for Mon–Thu dates that are
        NOT in this nurse's immediate pre/post weekend windows.
        """
        if date.weekday() not in (0, 1, 2, 3):
            return False
        if self._is_in_pre_weekend_window(nurse, date):
            return False
        if self._is_in_post_weekend_window(nurse, date):
            return False
        return True

    
    def _spread_components(self) -> tuple[int, int, int]:
        """
        Return (backup_spread, main_spread, total_spread).
        Primary objective: minimize backup spread.
        """
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts
        tot   = mains + backs
        s_b = int(backs.max() - backs.min()) if len(backs) else 0
        s_m = int(mains.max() - mains.min()) if len(mains) else 0
        s_t = int(tot.max()   - tot.min())   if len(tot)   else 0
        return (s_b, s_m, s_t)
    
    def _lexi_better(self, new_tuple: tuple[int, int, int], base_tuple: tuple[int, int, int]) -> bool:
        """Return True if new_tuple is lexicographically better than base_tuple."""
        return new_tuple < base_tuple

    
    def _get_weekend_dates(self, weekend_start: pd.Timestamp) -> List[pd.Timestamp]:
        """Get the three dates of a weekend (Friday, Saturday, Sunday)."""
        return [
            weekend_start,
            weekend_start + timedelta(days=1),
            weekend_start + timedelta(days=2),
        ]

    def _get_weekend_assignments(
        self, 
        fsf_nurse: str, 
        sfs_nurse: str
    ) -> List[Tuple[str, str]]:
        """Get the main/backup assignments for a weekend pattern."""
        return [
            (fsf_nurse, sfs_nurse),  # Fri (FSF main)
            (sfs_nurse, fsf_nurse),  # Sat (SFS main)
            (fsf_nurse, sfs_nurse),  # Sun (FSF main)
        ]

    def _apply_weekend_assignments(
        self,
        weekend_dates: List[pd.Timestamp],
        assignments: List[Tuple[str, str]]
    ) -> None:
        """Apply weekend assignments to schedule, respecting pre-scheduled slots."""
        for day, (main, backup) in zip(weekend_dates, assignments):
            if is_empty(self.state.schedule.at[day, "main"]):
                self.state.schedule.at[day, "main"] = main
            if is_empty(self.state.schedule.at[day, "backup"]):
                self.state.schedule.at[day, "backup"] = backup

    def _update_weekend_tracking(
        self, 
        weekend_start: pd.Timestamp, 
        fsf_nurse: str, 
        sfs_nurse: str
    ) -> None:
        """Update weekend tracking dictionary."""
        self.state.weekend_tracking[weekend_start] = (fsf_nurse, sfs_nurse)

    def _update_nurse_weekend_lists(
        self, 
        fsf_nurse: str, 
        sfs_nurse: str, 
        weekend_start: pd.Timestamp
    ) -> None:
        """Keep each nurse's sorted Friday list up-to-date (deduplicated)."""
        for nurse in (fsf_nurse, sfs_nurse):
            lst = self.state.nurse_weekend_lists.setdefault(nurse, [])
            pos = bisect.bisect_left(lst, weekend_start)
            if pos >= len(lst) or lst[pos] != weekend_start:
                bisect.insort(lst, weekend_start)

    def _update_pattern_tracking(self, fsf_nurse: str, sfs_nurse: str) -> None:
        """Update pattern tracking and count repeats."""
        prev_fsf = self.state.last_pattern.get(fsf_nurse, None)
        prev_sfs = self.state.last_pattern.get(sfs_nurse, None)

        # Ensure keys exist to avoid KeyError or skipped counts
        self.state.last_pattern.setdefault(fsf_nurse, None)
        self.state.last_pattern.setdefault(sfs_nurse, None)

        self.state.last_pattern[fsf_nurse] = WeekendPattern.FSF
        self.state.last_pattern[sfs_nurse] = WeekendPattern.SFS

        self.state.rotation_repeats += (
            int(prev_fsf == WeekendPattern.FSF) +
            int(prev_sfs == WeekendPattern.SFS)
        )

    def _log_assignment_debug(
        self,
        *,
        context: str,
        phase: str,
        date: pd.Timestamp | None,
        role: str | None,
        eligible: Iterable[str] | None,
        diagnostics: dict[str, list[str]] | None,
        final_pick: Optional[str],
        note: str | None = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """Emit a structured assignment log entry when debug mode is enabled."""

        if not ASSIGNMENT_DEBUG_LOGGER.enabled:
            return

        eligible_list = list(eligible or [])
        diagnostics_map = {
            nurse: list(reasons)
            for nurse, reasons in (diagnostics or {}).items()
        }

        def _series_to_int_dict(series: pd.Series) -> dict[str, int]:
            if series is None:
                return {}
            out: dict[str, int] = {}
            for key, value in series.items():
                if pd.isna(value):
                    out[str(key)] = 0
                else:
                    try:
                        out[str(key)] = int(value)
                    except (TypeError, ValueError):
                        out[str(key)] = int(float(value) if value is not None else 0)
            return out

        main_counts = _series_to_int_dict(self.state.main_assignment_counts)
        backup_counts = _series_to_int_dict(self.state.backup_assignment_counts)
        total_series = self.state.main_assignment_counts + self.state.backup_assignment_counts
        total_counts = _series_to_int_dict(total_series)

        hist_main = {str(k): int(v) for k, v in self.hist_main.items()}
        hist_backup = {str(k): int(v) for k, v in self.hist_backup.items()}

        candidate_stats = [
            {
                "nurse": nurse,
                "main_assignments": main_counts.get(nurse, 0),
                "backup_assignments": backup_counts.get(nurse, 0),
                "total_assignments": total_counts.get(nurse, 0),
                "history_main": hist_main.get(nurse, 0),
                "history_backup": hist_backup.get(nurse, 0),
            }
            for nurse in eligible_list
        ]

        payload = {
            "context": context,
            "phase": phase,
            "date": date.date().isoformat() if isinstance(date, pd.Timestamp) else None,
            "role": role,
            "eligible": eligible_list,
            "candidate_stats": candidate_stats,
            "rejections": diagnostics_map,
            "counts_main": main_counts,
            "counts_backup": backup_counts,
            "counts_total": total_counts,
            "history_main": hist_main,
            "history_backup": hist_backup,
            "final_pick": final_pick,
            "note": note,
            "extra": dict(extra) if extra else {},
        }

        ASSIGNMENT_DEBUG_LOGGER.log(payload)

    # ===== WEEKDAY ASSIGNMENT =====
    
    def assign_weekdays(self) -> None:
        """Assign nurses to all Mon-Thu slots, skipping pre-scheduled slots."""
        sched = self.state.schedule
        weekday_dates = sched.index[~sched['is_weekend']]

        for date in weekday_dates:
            self._debug_print(
                f"[ScheduleVariant] [WeekdayAssign] start {date.date()}"
            )
            self._assign_roles_for_date(date)

        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    def _assign_roles_for_date(self, date: pd.Timestamp) -> None:
        roles_and_counts = [
            ('main', self.state.main_assignment_counts),
            ('backup', self.state.backup_assignment_counts)
        ]
        for role, counts in roles_and_counts:
            if self._is_pre_scheduled(date, role):
                continue
            if not is_empty(self.state.schedule.at[date, role]):
                continue
            self._debug_print(
                f"[ScheduleVariant] [WeekdayAssign] {date.date()} role={role}"
            )

            diag_map: Optional[dict[str, list[str]]] = (
                {} if ASSIGNMENT_DEBUG_LOGGER.enabled else None
            )

            # 1) Base rules
            eligible = self._get_eligible_nurses_for_day(
                date,
                role,
                diagnostics=diag_map if diag_map is not None else None,
                relaxed_spacing=False,
            )

            used_relaxed = False

            # 2) Fallback to relaxed spacing ONLY if nothing is base-eligible
            if not eligible and self.config.allow_one_day_weekday_gap:
                diag_relaxed: Optional[dict[str, list[str]]] = (
                    {} if ASSIGNMENT_DEBUG_LOGGER.enabled else None
                )
                eligible = self._get_eligible_nurses_for_day(
                    date,
                    role,
                    diagnostics=diag_relaxed if diag_relaxed is not None else None,
                    relaxed_spacing=True,
                )
                if diag_relaxed is not None:
                    diag_map = diag_relaxed
                used_relaxed = True
                self._debug_print(
                    f"[ScheduleVariant] [WeekdayAssign] relaxed {date.date()} role={role}"
                )

            diag_for_log = diag_map or {}

            self._log_assignment_debug(
                context="weekday_assign",
                phase="pre_select",
                date=date,
                role=role,
                eligible=eligible,
                diagnostics=diag_for_log,
                final_pick=None,
                note="relaxed_spacing" if used_relaxed else None,
                extra={
                    "relaxed_spacing": used_relaxed,
                    "eligible_count": len(eligible),
                },
            )

            if not eligible:
                self._debug_print(
                    f"[ScheduleVariant] [WeekdayAssign] fail {date.date()} role={role}"
                )
                self.state.schedule.at[date, role] = None
                self._log_assignment_debug(
                    context="weekday_assign",
                    phase="post_select",
                    date=date,
                    role=role,
                    eligible=eligible,
                    diagnostics=diag_for_log,
                    final_pick=None,
                    note="no_candidate",
                    extra={
                        "relaxed_spacing": used_relaxed,
                        "eligible_count": 0,
                        "assignment_success": False,
                    },
                )
                continue

            pick = self._select_best_candidate(eligible, role, date)
            self.state.schedule.at[date, role] = pick
            counts[pick] += 1
            self.state.last_assignment[pick] = date

            self._log_assignment_debug(
                context="weekday_assign",
                phase="post_select",
                date=date,
                role=role,
                eligible=eligible,
                diagnostics=diag_for_log,
                final_pick=pick,
                note="relaxed_spacing" if used_relaxed else None,
                extra={
                    "relaxed_spacing": used_relaxed,
                    "eligible_count": len(eligible),
                    "assignment_success": True,
                },
            )

    def _get_eligible_nurses_for_day(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        relaxed_spacing: bool = False
    ) -> list[str]:
        sched = self.state.schedule
        avail = self.availability.loc[date]
        other_role = 'backup' if role == 'main' else 'main'
        other_nurse = sched.at[date, other_role]

        out: list[str] = []
        for nurse in self.nurses:
            reasons: list[str] = []
            reasons_arg = reasons if diagnostics is not None else None

            if not self._passes_basic_eligibility_checks(
                nurse, date, role, avail, other_nurse, diagnostics=reasons_arg
            ):
                if diagnostics is not None:
                    diagnostics[nurse] = list(reasons)
                continue
            if not self._passes_advanced_eligibility_checks(
                nurse,
                date,
                role,
                relaxed_spacing=relaxed_spacing,
                diagnostics=reasons_arg,
            ):
                if diagnostics is not None:
                    diagnostics[nurse] = list(reasons)
                continue
            if diagnostics is not None:
                diagnostics[nurse] = list(reasons)
            out.append(nurse)
        return out

    # ────────────────────────────────────────────────────────────────────
    # 1.  BASIC ELIGIBILITY (fixes NaN availability handling)
    # ────────────────────────────────────────────────────────────────────
    def _passes_basic_eligibility_checks(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        avail: pd.Series,
        other_nurse: str,
        diagnostics: Optional[list[str]] = None
    ) -> bool:
        """
        Quick rejects before the heavier spacing / weekly-limit checks.
    
        * Treat **NaN / missing** cells in the availability matrix
          as **unavailable** (previously they slipped through).
        """
        return passes_basic_eligibility_checks(
            nurse=nurse,
            avail_row=avail,
            other_nurse=other_nurse,
            has_late_shift_conflict=self._late_shift_conflict(nurse, date, role),
            diagnostics=diagnostics,
        )

    def _passes_advanced_eligibility_checks(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        *,
        relaxed_spacing: bool = False,
        diagnostics: Optional[list[str]] = None
    ) -> bool:
        if not self._has_sufficient_spacing(nurse, date, role, relaxed_spacing=relaxed_spacing):
            if diagnostics is not None:
                diagnostics.append("insufficient_spacing")
            return False
        if not self._validate_weekly_assignment_limits(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekly_limit")
            return False
        if not self._validate_weekday_relative_to_weekend(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekend_window")
            return False
        return True

    # ────────────────────────────────────────────────────────────────────
    # 2.  FULL-ROW ELIGIBILITY (also fixes NaN handling)
    # ────────────────────────────────────────────────────────────────────
    def _is_nurse_eligible_for_assignment(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        *,
        relaxed_spacing: bool = False
    ) -> bool:
        """Mon–Thu eligibility (NaN-safe availability + advanced rules)."""
        if self._late_shift_conflict(nurse, date, role):
            return False
        if date not in self.state.schedule.index:
            return False
        try:
            val = self.availability.at[date, nurse]
            if pd.isna(val) or not bool(val):
                return False
        except KeyError:
            return False
        return self._passes_advanced_eligibility_checks(
            nurse, date, role, relaxed_spacing=relaxed_spacing
        )

    def _is_nurse_eligible_for_assignment_gap(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        *,
        relaxed_spacing: bool = False,
        diagnostics: Optional[list[str]] = None
    ) -> bool:
        """
        Gap-filling eligibility (NaN-safe) with Mon/Tue pre-weekend relaxation already
        in your code.  The 'relaxed_spacing' flag ONLY affects spacing distance,
        not the pre/post-weekend day-of-week allowances.
        """
        if self._late_shift_conflict(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("late_shift_conflict")
            return False
        if date not in self.state.schedule.index:
            if diagnostics is not None:
                diagnostics.append("date_out_of_range")
            return False
        try:
            val = self.availability.at[date, nurse]
            if pd.isna(val) or not bool(val):
                if diagnostics is not None:
                    diagnostics.append("unavailable")
                return False
        except KeyError:
            if diagnostics is not None:
                diagnostics.append("unavailable")
            return False

        if not self._has_sufficient_spacing(nurse, date, role, relaxed_spacing=relaxed_spacing):
            if diagnostics is not None:
                diagnostics.append("insufficient_spacing")
            return False
        if not self._validate_weekly_assignment_limits(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekly_limit")
            return False
        if not self._validate_weekday_relative_to_weekend_gap(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekend_window")
            return False
        return True

    # ===== ASSIGNMENT MANAGEMENT =====
    
    def _inc_assign(
        self, 
        date: pd.Timestamp, 
        role: str, 
        nurse: str, 
        gap_phase: bool = False
    ) -> bool:
        """Assign nurse to schedule and update counters (with relaxed fallback if needed)."""
        if self._is_pre_scheduled(date, role):
            return False
    
        # Pick the right checker
        check = (self._is_nurse_eligible_for_assignment_gap 
                 if gap_phase else self._is_nurse_eligible_for_assignment)
    
        # 1) Try base spacing first
        ok = check(nurse, date, role, relaxed_spacing=False)
    
        # 2) If base fails, optionally try relaxed spacing (only Mon–Thu and
        #    only when not in this nurse's pre/post weekend windows)
        if (not ok 
            and self.config.allow_one_day_weekday_gap 
            and self._weekday_relaxation_applicable(nurse, date)):
            ok = check(nurse, date, role, relaxed_spacing=True)
    
        if not ok:
            return False
    
        apply_assignment(
            schedule_df=self.state.schedule,
            main_counts=self.state.main_assignment_counts,
            backup_counts=self.state.backup_assignment_counts,
            last_assignment=self.state.last_assignment,
            date=date,
            role=role,
            nurse=nurse,
        )

        self._invalidate_weekday_cache()
        return True

    # ────────────────────────────────────────────────────────────────────
    # 4.  ASSIGNMENT REMOVAL (fixes negative counters)
    # ────────────────────────────────────────────────────────────────────
    def _dec_assign(self, date: pd.Timestamp, role: str, nurse: str) -> None:
        """
        Roll back an assignment and keep counters *non-negative*.
        """
        if self._is_pre_scheduled(date, role):
            return
    
        revert_assignment(
            schedule_df=self.state.schedule,
            main_counts=self.state.main_assignment_counts,
            backup_counts=self.state.backup_assignment_counts,
            last_assignment=self.state.last_assignment,
            mutation=AssignmentMutation(
                date=date,
                role=role,
                previous_nurse=None,
                next_nurse=nurse,
            ),
        )

        self._invalidate_weekday_cache()
            
    # ===== NURSE SELECTION =====
    
    def _select_best_candidate(self, nurses: list[str], role: str, date: pd.Timestamp | None = None) -> str:
        """
        Deterministic tie-breaking among 'nurses' for 'role':
          1) fewest role-specific assignments (backup if role=='backup', main otherwise)
          1.5) FEWEST assignments on this same weekday (Mon–Thu) so far (soft preference)
          2) fewest total assignments (main+backup)
          3) fewest 30-day total (main + backup)
          4) first by canonical global order (self.nurses)
        """
        role_counts  = self.state.main_assignment_counts if role == "main" else self.state.backup_assignment_counts
        total_counts = self._get_total_counts()
    
        # Optional weekday diversity counts (Mon–Thu only)
        if date is not None and date.weekday() in (0, 1, 2, 3):
            w_counts = self._weekday_counts_for(int(date.weekday()))
        else:
            w_counts = {}
    
        # 1) fewest role-specific count
        min_role = min(role_counts[n] for n in nurses)
        tier = [n for n in nurses if role_counts[n] == min_role]
        if len(tier) == 1:
            return tier[0]
    
        # 1.5) fewest same-weekday count (soft)
        if w_counts:
            min_dow = min(w_counts.get(n, 0) for n in tier)
            tier_dow = [n for n in tier if w_counts.get(n, 0) == min_dow]
        else:
            tier_dow = tier
        if len(tier_dow) == 1:
            return tier_dow[0]
    
        # 2) fewest total
        min_total = min(total_counts[n] for n in tier_dow)
        tier2 = [n for n in tier_dow if total_counts[n] == min_total]
        if len(tier2) == 1:
            return tier2[0]
    
        # 3) fewest 30-day history
        def past_total(n: str) -> int:
            return self.hist_main.get(n, 0) + self.hist_backup.get(n, 0)
        min_hist = min(past_total(n) for n in tier2)
        tier3 = [n for n in tier2 if past_total(n) == min_hist]
        if len(tier3) == 1:
            return tier3[0]
    
        # 4) deterministic fallback by canonical order
        order = self._order_index
        return min(tier3, key=lambda n: order.get(n, 1_000_000))

    # ===== VALIDATION METHODS =====
    
    def _has_sufficient_spacing(
        self, 
        nurse: str, 
        date: pd.Timestamp, 
        role: str,
        *,
        relaxed_spacing: bool = False
    ) -> bool:
        """
        Base: require self.config.min_days_between_assignments days clear on
        both sides.  If relaxed_spacing is True AND the relaxation is applicable
        for this nurse/date, we enforce only a one-day clear buffer.
        """
        base = int(self.config.min_days_between_assignments)
        min_days_off = base
    
        if (relaxed_spacing 
            and self.config.allow_one_day_weekday_gap 
            and self._weekday_relaxation_applicable(nurse, date)):
            # One-day buffer: forbid assignments on adjacent days only
            min_days_off = max(1, base - 1)
    
        idx_set = self._get_index_set()
        for offset in range(1, min_days_off + 1):
            for check_date in (date - timedelta(days=offset), date + timedelta(days=offset)):
                if check_date in idx_set:
                    if self._nurse_assigned_on_date(nurse, check_date):
                        return False
        return True

    def _nurse_assigned_on_date(self, nurse: str, date: pd.Timestamp) -> bool:
        """Check if nurse is assigned on specific date."""
        sched = self.state.schedule
        return (nurse == sched.at[date, 'main'] or 
                nurse == sched.at[date, 'backup'])

    def _validate_weekly_assignment_limits(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str
    ) -> bool:
        """Check weekly assignment limits."""
        week_start = date - timedelta(days=date.weekday())  # Always Monday
        idx_set = self._get_index_set()
        sched = self.state.schedule

        main_count = 0
        backup_count = 0
        for i in range(4):  # Mon–Thu
            d = week_start + timedelta(days=i)
            if d == date or d not in idx_set:
                continue
            if sched.at[d, 'main'] == nurse:
                main_count += 1
            if sched.at[d, 'backup'] == nurse:
                backup_count += 1
        total_count = main_count + backup_count

        if role == 'main' and main_count >= MAX_MAIN_ASSIGNMENTS_PER_WEEK:
            return False
        if total_count >= MAX_TOTAL_ASSIGNMENTS_PER_WEEK:
            return False
    
        return True
        
    def _validate_weekday_relative_to_weekend(
        self, 
        nurse: str, 
        date: pd.Timestamp, 
        role: str
    ) -> bool:
        """Validate weekday assignments relative to weekend schedule."""
        cfg = WeekdayConstraintConfig(
            allow_post_weekend_wednesday_main=self.config.allow_post_weekend_wednesday_main,
            allow_post_weekend_wednesday_backup=self.config.allow_post_weekend_wednesday_backup,
            allow_post_weekend_thursday_main=self.config.allow_post_weekend_thursday_main,
            allow_post_weekend_thursday_backup=self.config.allow_post_weekend_thursday_backup,
        )
        return validate_weekday_relative_to_weekend(
            nurse=nurse,
            date=date,
            role=role,
            config=cfg,
            is_in_pre_weekend_window=self._is_in_pre_weekend_window,
            is_in_post_weekend_window=self._is_in_post_weekend_window,
        )

    def _validate_post_weekend_assignment(
        self, 
        weekday: int, 
        role: str, 
        cfg: 'SchedulerConfig'
    ) -> bool:
        """Validate post-weekend assignment based on day and role."""
        return validate_post_weekend_assignment(
            weekday=weekday,
            role=role,
            config=WeekdayConstraintConfig(
                allow_post_weekend_wednesday_main=cfg.allow_post_weekend_wednesday_main,
                allow_post_weekend_wednesday_backup=cfg.allow_post_weekend_wednesday_backup,
                allow_post_weekend_thursday_main=cfg.allow_post_weekend_thursday_main,
                allow_post_weekend_thursday_backup=cfg.allow_post_weekend_thursday_backup,
            ),
        )

    def _validate_weekday_relative_to_weekend_gap(
        self, 
        nurse: str, 
        date: pd.Timestamp, 
        role: str
    ) -> bool:
        """Gap-filling specific weekend validation with relaxed Monday/Tuesday rule."""
        cfg = WeekdayConstraintConfig(
            allow_post_weekend_wednesday_main=self.config.allow_post_weekend_wednesday_main,
            allow_post_weekend_wednesday_backup=self.config.allow_post_weekend_wednesday_backup,
            allow_post_weekend_thursday_main=self.config.allow_post_weekend_thursday_main,
            allow_post_weekend_thursday_backup=self.config.allow_post_weekend_thursday_backup,
        )
        return validate_weekday_relative_to_weekend_gap(
            nurse=nurse,
            date=date,
            role=role,
            config=cfg,
            is_in_pre_weekend_window=self._is_in_pre_weekend_window,
            is_in_post_weekend_window=self._is_in_post_weekend_window,
        )

    # ===== WEEKEND WINDOW DETECTION =====
    
    def _get_neighboring_fridays(
        self, 
        nurse: str, 
        current_date: pd.Timestamp
    ) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        """Get the previous and next Friday for a nurse."""
        fridays = self.state.nurse_weekend_lists.get(nurse, [])
        idx = bisect.bisect_left(fridays, current_date)
        prev_fri = fridays[idx-1] if idx > 0 else None
        next_fri = fridays[idx] if idx < len(fridays) else None
        return prev_fri, next_fri

    def _is_in_pre_weekend_window(
        self, 
        nurse: str, 
        date: pd.Timestamp, 
        window: int = DEFAULT_PRE_WEEKEND_WINDOW
    ) -> bool:
        """Check if date is in pre-weekend window."""
        _, next_fri = self._get_neighboring_fridays(nurse, date)
        if next_fri is None:
            return False
        days = (next_fri - date).days
        return 1 <= days <= window

    def _is_in_post_weekend_window(
        self,
        nurse: str,
        date: pd.Timestamp,
        window: int = DEFAULT_POST_WEEKEND_WINDOW
    ) -> bool:
        """Check if date is in post-weekend window."""
        prev_fri, _ = self._get_neighboring_fridays(nurse, date)
        if prev_fri is None:
            return False
        days = (date - prev_fri).days
        return 1 <= days <= window

    def _invalidate_weekday_cache(self) -> None:
        """Clear cached per-weekday assignment counts and total counts."""
        self._weekday_counts_cache.clear()
        self._total_counts_cache = None

    def _get_total_counts(self) -> pd.Series:
        """Return cached main+backup total counts (invalidated with weekday cache)."""
        if self._total_counts_cache is None:
            self._total_counts_cache = self.state.main_assignment_counts + self.state.backup_assignment_counts
        return self._total_counts_cache

    def _get_index_set(self) -> frozenset:
        """Return cached frozenset of schedule index dates for O(1) membership."""
        if not hasattr(self, '_index_set') or self._index_set is None:
            self._index_set = frozenset(self.state.schedule.index)
        return self._index_set

    def _weekday_counts_for(self, weekday: int) -> dict[str, int]:
        """
        Return total assignments per nurse on the given weekday (Mon–Thu) in the
        current schedule (main + backup). Used as a soft tie-breaker so we avoid
        giving the same nurse too many of the same weekday across the period.
        """
        if weekday not in (0, 1, 2, 3):  # only Mon–Thu
            return {}
        cached = self._weekday_counts_cache.get(weekday)
        if cached is not None:
            return cached

        sched = self.state.schedule
        sub = sched.loc[~sched["is_weekend"], ["main", "backup"]]
        sub = sub[sub.index.weekday == weekday]
        if sub.empty:
            self._weekday_counts_cache[weekday] = {}
            return {}
        m = sub["main"].value_counts()
        b = sub["backup"].value_counts()
        counts = m.add(b, fill_value=0).astype(int)
        result = counts.to_dict()
        self._weekday_counts_cache[weekday] = result
        return result

    
    # ===== SCHEDULE UTILITY METHODS =====
    
    def _recalculate_assignment_counts(self) -> None:
        """Recalculate assignment counts from current schedule."""
        self._invalidate_weekday_cache()
        mains = self.state.schedule['main'].value_counts()
        backups = self.state.schedule['backup'].value_counts()

        for nurse in self.state.main_assignment_counts.index:
            self.state.main_assignment_counts[nurse] = mains.get(nurse, 0)
            self.state.backup_assignment_counts[nurse] = backups.get(nurse, 0)

    def _update_last_assignment_dates(self) -> None:
        """Recompute last-assignment exactly from the schedule (can move backward)."""
        sched = self.state.schedule
        # Iterate over a stable list of keys in case callers mutate the dict elsewhere
        for nurse in list(self.state.last_assignment.keys()):
            assigned = sched.index[
                (sched["main"] == nurse) | (sched["backup"] == nurse)
            ]
            self.state.last_assignment[nurse] = assigned.max() if len(assigned) else None
    
    def _days_to(self, start: pd.Timestamp, end: pd.Timestamp) -> int:
        """Return number of days between dates."""
        return (end - start).days

    # ===== CONVENIENCE METHODS =====
    
    def count_gaps(self) -> int:
        """Count empty slots in schedule."""
        return _count_main_backup_empties(self.state.schedule)

    def get_weekdays(self) -> List[pd.Timestamp]:
        """Get all weekday dates from schedule (cached; index never changes)."""
        if not hasattr(self, '_weekdays_cache') or self._weekdays_cache is None:
            self._weekdays_cache = [
                d for d in self.state.schedule.index
                if not self.state.schedule.at[d, 'is_weekend']
            ]
        return self._weekdays_cache

    def get_weeks(self) -> List[List[pd.Timestamp]]:
        """Get weekday dates grouped by week (cached; index never changes)."""
        if not hasattr(self, '_weeks_cache') or self._weeks_cache is None:
            weekdays = self.get_weekdays()
            weeks = []
            seen = set()

            for date in weekdays:
                week_start = date - timedelta(days=date.weekday())
                if week_start not in seen:
                    week_days = [
                        week_start + timedelta(days=i) for i in range(4)
                        if (week_start + timedelta(days=i)) in weekdays
                    ]
                    weeks.append(week_days)
                    seen.add(week_start)

            self._weeks_cache = weeks
        return self._weeks_cache

    def calculate_imbalance(self) -> int:
        """Calculate total assignment imbalance across all nurses."""
        main_counts = self.state.main_assignment_counts.values
        backup_counts = self.state.backup_assignment_counts.values
        
        imbalance_main = (max(main_counts) - min(main_counts)) if len(main_counts) > 0 else 0
        imbalance_backup = (max(backup_counts) - min(backup_counts)) if len(backup_counts) > 0 else 0
        
        return imbalance_main + imbalance_backup

    def _rebalance_score(self, alpha: float = 0.5, beta: float = 1.5) -> float:
        """Calculate rebalancing objective score."""
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts
        
        main_spread = int(mains.max() - mains.min())
        backup_spread = int(backs.max() - backs.min())
        
        total = mains + backs
        total_spread = int(total.max() - total.min())
        
        return main_spread + beta * backup_spread + alpha * total_spread

    # ===== ASSIGNMENT METHODS =====
    
    def _assign_slot_sequence(
        self,
        slots: Iterable[tuple[pd.Timestamp, str]],
        *,
        gap_mode: bool = False,
        update_state: bool = True,
        allow_partial: bool = False,
        force_relaxed: bool = False,
    ) -> bool:
        """Assign nurses to an ordered sequence of ``(date, role)`` slots."""

        domain_fn: Callable[[pd.Timestamp, str], list[str]]
        if gap_mode:
            domain_fn = lambda d, r: self._eligible_domain_gap(d, r, force_relaxed=force_relaxed)
        else:
            domain_fn = lambda d, r: self._eligible_domain(d, r, force_relaxed=force_relaxed)

        had_failure = False

        for date, role in slots:
            if self._is_pre_scheduled(date, role):
                continue
            if not is_empty(self.state.schedule.at[date, role]):
                continue
            self._debug_print(
                f"[ScheduleVariant] [SlotSeq] slot {date.date()} role={role}"
            )

            domain = domain_fn(date, role)
            if not domain:
                self._debug_print(
                    f"[ScheduleVariant] [SlotSeq] empty {date.date()} role={role} gap={gap_mode}"
                )
                if allow_partial:
                    had_failure = True
                    continue
                return False

            placed = False
            for nurse in domain:
                if self._inc_assign(date, role, nurse, gap_phase=gap_mode):
                    placed = True
                    break

            if not placed:
                self._debug_print(
                    f"[ScheduleVariant] [SlotSeq] fail {date.date()} role={role} gap={gap_mode}"
                )
                if allow_partial:
                    had_failure = True
                    continue
                return False

        if update_state:
            self._recalculate_assignment_counts()
            self._update_last_assignment_dates()
        return not had_failure

    def assign_nurses_to_weekdays(
        self,
        weekdays: list[pd.Timestamp],
        role: str,
        sorted_nurses: list[str] | None = None
    ) -> bool:
        """
        Assign nurses to the given weekdays for ``role`` using live counts.

        This method now simply builds a sequence of slots and defers to
        :meth:`_assign_slot_sequence`, preserving the dynamic candidate
        ordering used during weekday assignment.
        """
        slots = [(date, role) for date in weekdays]
        return self._assign_slot_sequence(slots)

    def _try_assign_first_eligible(
        self, 
        date: pd.Timestamp, 
        role: str, 
        sorted_nurses: List[str]
    ) -> bool:
        """Try to assign first eligible nurse to date/role."""
        other_role = "backup" if role == "main" else "main"
        
        for nurse in sorted_nurses:
            if self.state.schedule.at[date, other_role] == nurse:
                continue
            if not self._is_nurse_eligible_for_assignment(nurse, date, role):
                continue
                
            self.state.schedule.at[date, role] = nurse
            if role == "main":
                self.state.main_assignment_counts[nurse] += 1
            else:
                self.state.backup_assignment_counts[nurse] += 1
            self.state.last_assignment[nurse] = date
            return True
        
        return False

    def _get_eligible_nurses_for_day_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        relaxed_spacing: bool = False
    ) -> list[str]:
        other_role = 'backup' if role == 'main' else 'main'
        other_nurse = self.state.schedule.at[date, other_role]
        capture = diagnostics
        created_local_diag = False
        if capture is None and ASSIGNMENT_DEBUG_LOGGER.enabled:
            capture = {}
            created_local_diag = True

        try:
            avail_row = self.availability.loc[date]
        except KeyError:
            avail_row = pd.Series(dtype="object")

        out: list[str] = []
        for nurse in self.nurses:
            reasons: list[str] = []
            reasons_arg = reasons if capture is not None else None

            if not self._passes_basic_eligibility_checks(
                nurse,
                date,
                role,
                avail_row,
                other_nurse,
                diagnostics=reasons_arg,
            ):
                if capture is not None:
                    capture[nurse] = list(reasons)
                continue

            if not self._has_sufficient_spacing(
                nurse,
                date,
                role,
                relaxed_spacing=relaxed_spacing,
            ):
                if capture is not None:
                    reasons.append("insufficient_spacing")
                    capture[nurse] = list(reasons)
                continue

            if not self._validate_weekly_assignment_limits(nurse, date, role):
                if capture is not None:
                    reasons.append("weekly_limit")
                    capture[nurse] = list(reasons)
                continue

            if not self._validate_weekday_relative_to_weekend_gap(nurse, date, role):
                if capture is not None:
                    reasons.append("weekend_window")
                    capture[nurse] = list(reasons)
                continue

            if capture is not None:
                capture[nurse] = list(reasons)
            out.append(nurse)
        if ASSIGNMENT_DEBUG_LOGGER.enabled:
            diag_for_log = capture or {}
            self._log_assignment_debug(
                context="gap_candidates",
                phase="candidate_pool",
                date=date,
                role=role,
                eligible=out,
                diagnostics=diag_for_log,
                final_pick=None,
                note="relaxed_spacing" if relaxed_spacing else None,
                extra={
                    "relaxed_spacing": relaxed_spacing,
                    "eligible_count": len(out),
                },
            )
        if created_local_diag:
            capture = None
        return out

    # ===== NEXT WEEKEND UTILITY =====
    
    def _get_next_weekend_dates(
        self, 
        current_date: pd.Timestamp
    ) -> List[pd.Timestamp]:
        """Get the dates of the next weekend."""
        days_ahead = (FRIDAY_WEEKDAY - current_date.weekday()) % 7
        friday = current_date + timedelta(days=days_ahead)
        return [friday + timedelta(days=i) for i in range(DAYS_IN_WEEKEND)]

    def _is_nurse_assigned_to_next_weekend(
        self, 
        nurse: str, 
        current_date: pd.Timestamp
    ) -> bool:
        """Check if nurse is assigned to next weekend."""
        weekend_dates = self._get_next_weekend_dates(current_date)
        return any(
            d in self.state.schedule.index and 
            (self.state.schedule.at[d, 'main'] == nurse or 
             self.state.schedule.at[d, 'backup'] == nurse)
            for d in weekend_dates
        )

    # ===== BACKUP AND RESTORE =====
    
    def backup_week_assignments(
        self, 
        week_days: List[pd.Timestamp]
    ) -> 'WeekBackup':
        """Return a snapshot of week assignments for restoration."""
        return WeekBackup(
            rows=self.state.schedule.loc[week_days, ['main', 'backup']].copy(),
            main_counts=self.state.main_assignment_counts.copy(),
            backup_counts=self.state.backup_assignment_counts.copy(),
            last_assignment=dict(self.state.last_assignment),
        )

    # ===== REBALANCING =====
    
    def iterative_rebalance_no_revert(
        self,
        tolerance: int = 1,
        max_iterations: int = 6000,
        alpha: float = 1.0,
        beta: float = 1.0,
        #early_stop_spread: tuple[int, int] | None = (1, 1),
        early_stop_spread=None,
        tracker: Optional[BestStateTracker] = None,
    ) -> bool:
        """
        Rebalance Monday-Thursday weeks in place. Early-stop when both spreads
        are <= early_stop_spread.
        """
        created_tracker = tracker is None
        if created_tracker:
            tracker = BestStateTracker(self)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.begin_iteration("[Rebalance]")

        improved = False

        if early_stop_spread:
            s_b, s_m, _ = self._spread_components()
            if s_b <= early_stop_spread[0] and s_m <= early_stop_spread[1]:
                return True

        for iteration in range(max_iterations):
            tracker.begin_iteration(f"[Rebalance] Iter {iteration}")

            schedule_changed = self._rebalance_all_weeks()

            comparison = tracker.evaluate_and_commit(
                phase_name=f"[Rebalance] Iter {iteration}",
                allow_neutral=True,
            )
            if comparison == Comparison.BETTER:
                improved = True

            if early_stop_spread:
                s_b, s_m, _ = self._spread_components()
                if s_b <= early_stop_spread[0] and s_m <= early_stop_spread[1]:
                    tracker.restore_global_best()
                    print(f"[Rebalance] Target reached at iter {iteration}")
                    return True

            if not schedule_changed and comparison != Comparison.BETTER:
                print(f"[Rebalance] No progress at iter {iteration}")
                break

        tracker.restore_global_best()
        final_quality = tracker.get_global_best_quality()

        if final_quality and initial_quality:
            improved = final_quality.is_better_than(initial_quality) or improved

        print(
            f"[Rebalance] Final: {initial_quality} -> {final_quality} "
            f"(improved={bool(improved)})"
        )
        print(f"[Rebalance] Statistics: {tracker.get_statistics()}")

        return bool(improved)

    def _spread_main_backup(self) -> tuple[int, int]:
        """Return (spread_main, spread_backup)."""
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts
        s_m = int(mains.max() - mains.min()) if len(mains) else 0
        s_b = int(backs.max() - backs.min()) if len(backs) else 0
        return s_m, s_b
    
    def _collect_weekday_windows(self, window_weeks: int = 2) -> list[list[pd.Timestamp]]:
        """
        Build sliding windows of 'window_weeks' consecutive Monday–Thursday blocks.
        Returns a list of lists of dates (Mon–Thu per week, concatenated).
        """
        idx = self.state.schedule.index
        if len(idx) == 0:
            return []
        first = idx.min()
        last = idx.max()
    
        # first Monday >= first
        offset = (MONDAY_WEEKDAY - first.weekday()) % 7
        cur = first + timedelta(days=offset)
    
        windows: list[list[pd.Timestamp]] = []
        while cur <= last:
            days: list[pd.Timestamp] = []
            anchor = cur
            for _ in range(window_weeks):
                for i in range(4):  # Mon..Thu
                    d = anchor + timedelta(days=i)
                    if d in idx and not bool(self.state.schedule.at[d, "is_weekend"]):
                        days.append(d)
                anchor += timedelta(days=7)
            if len(days) >= 2:
                windows.append(days)
            cur += timedelta(days=7)
        return windows
    
    def _clear_window_assignments(self, days: list[pd.Timestamp]) -> None:
        """
        Clear non-pre-scheduled assignments for the given 'days' for both roles.
        Uses _dec_assign to keep counts/last_assignment consistent.
        """
        for d in days:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                nurse = self.state.schedule.at[d, role]
                if not is_empty(nurse):
                    self._dec_assign(d, role, nurse)
    
    def _eligible_domain(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self.domain_builder.eligible_domain(
            date,
            role,
            diagnostics=diagnostics,
            force_relaxed=force_relaxed,
            gap_mode=False,
        )

    def _eligible_domain_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self.domain_builder.eligible_domain(
            date,
            role,
            diagnostics=diagnostics,
            force_relaxed=force_relaxed,
            gap_mode=True,
        )

    def _build_window_varlist(self, days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, str]]:
        """
        Variables to assign in the window: all (day, role) pairs that are empty and not pre-scheduled.
        """
        vars_list: list[tuple[pd.Timestamp, str]] = []
        for d in days:
            for role in ("main", "backup"):
                if not self._is_pre_scheduled(d, role):
                    val = self.state.schedule.at[d, role]
                    if is_empty(val):
                        vars_list.append((d, role))
        return vars_list
    
    def _backtrack_window(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
        *,
        gap_mode: bool = False,
        force_relaxed: bool = False,
        depth: int = 0,
    ) -> bool:
        return self.window_optimizer.backtrack_window(
            vars_list,
            deadline,
            node_budget,
            gap_mode=gap_mode,
            force_relaxed=force_relaxed,
            depth=depth,
        )
    
    def iterative_window_refill_rebalance(
        self,
        window_weeks: int = 3,
        max_passes: int = 6000,
        time_limit_ms: int = 800000,
        node_limit: int = 8000000,
        target_spread: tuple[int, int] | None = (1, 1),
        tracker: Optional[BestStateTracker] = None,
    ) -> bool:
        return self.window_optimizer.iterative_window_refill_rebalance(
            window_weeks=window_weeks,
            max_passes=max_passes,
            time_limit_ms=time_limit_ms,
            node_limit=node_limit,
            target_spread=target_spread,
            tracker=tracker,
        )

    def _get_all_weekdays(self) -> list[pd.Timestamp]:
        """All Mon–Thu dates in the schedule period (non-weekend)."""
        idx = self.state.schedule.index
        return [d for d in idx if not bool(self.state.schedule.at[d, "is_weekend"])]
    
    def _build_full_varlist(self, days: list[pd.Timestamp], role_order: str = "MB") -> list[tuple[pd.Timestamp, str]]:
        """
        Build a variable list over the entire period's weekdays.
        role_order "MB" → assign MAIN then BACKUP for each day; "BM" → BACKUP then MAIN.
        Skips pre-scheduled cells that already have fixed nurses.
        """
        return self.order_generator.build_full_varlist(days, role_order=role_order)
    
    def _gen_full_orders(
        self,
        days: list[pd.Timestamp],
        max_orders: int = 50,
    ) -> list[list[tuple[pd.Timestamp, str]]]:
        return self.order_generator.gen_full_orders(days, max_orders=max_orders)
    
    def _backtrack_full_order(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
    ) -> bool:
        """
        Backtracking that follows the given fixed variable order vars_list.
        Requires fully assigning all variables in vars_list for success.
        """
        n = len(vars_list)
    
        def dfs(idx: int) -> bool:
            if time.perf_counter() >= deadline or node_budget[0] <= 0:
                return False
    
            # advance to next unfilled
            while idx < n:
                d, r = vars_list[idx]
                val = self.state.schedule.at[d, r]
                if is_empty(val):
                    break
                idx += 1
            if idx >= n:
                return True
    
            d0, r0 = vars_list[idx]
            dom = self._eligible_domain(d0, r0)
            if not dom:
                return False  # must assign this var
    
            for nurse in dom:
                node_budget[0] -= 1
                if node_budget[0] <= 0 or time.perf_counter() >= deadline:
                    return False
    
                if not self._inc_assign(d0, r0, nurse):
                    continue
    
                # bounded forward check
                fail = False
                look_ahead = 8
                j = idx + 1
                steps = 0
                while j < n and steps < look_ahead:
                    dj, rj = vars_list[j]
                    if is_empty(self.state.schedule.at[dj, rj]):
                        if not self._eligible_domain(dj, rj):
                            fail = True
                            break
                        steps += 1
                    j += 1
    
                if not fail and dfs(idx + 1):
                    return True
    
                self._dec_assign(d0, r0, nurse)
    
            return False
    
        return dfs(0)
    
    def iterative_full_period_refill(
        self,
        max_orders: int = 10000,
        per_attempt_time_ms: int = 45000,
        per_attempt_nodes: int = 350000,
        target_spread: tuple[int, int] = (1, 1),
        required_spread: bool = True,
        tracker: Optional[BestStateTracker] = None,
    ) -> bool:
        return self.window_optimizer.iterative_full_period_refill(
            max_orders=max_orders,
            per_attempt_time_ms=per_attempt_time_ms,
            per_attempt_nodes=per_attempt_nodes,
            target_spread=target_spread,
            required_spread=required_spread,
            tracker=tracker,
        )
    
    def _rebalance_all_weeks(self) -> bool:
        """Rebalance all Monday-Thursday weeks."""
        schedule_changed = False
        idx = self.state.schedule.index
        if len(idx) == 0:
            return False
    
        first = idx.min()
        last = idx.max()
    
        # Find the first Monday on/after the first date
        offset = (MONDAY_WEEKDAY - first.weekday()) % 7
        current = first + timedelta(days=offset)
    
        while current <= last:
            week_days = self._get_week_days(current)
            if week_days:
                changed = self._try_week_permutations_no_revert(week_days)
                schedule_changed |= changed
            current += timedelta(days=7)
    
        return schedule_changed
        
    def _get_week_days(self, monday: pd.Timestamp) -> List[pd.Timestamp]:
        """Get Monday–Thursday dates for a given Monday that exist in the index (non-weekend)."""
        idx = self.state.schedule.index
        sched = self.state.schedule
        days: List[pd.Timestamp] = []
        for i in range(4):  # Mon..Thu
            d = monday + timedelta(days=i)
            if d in idx and not bool(sched.at[d, "is_weekend"]):
                days.append(d)
        return days

    def _is_within_tolerance(self, tolerance: int) -> bool:
        """Check if current assignment distribution is within tolerance."""
        mains_sum = self.state.main_assignment_counts.sum()
        backs_sum = self.state.backup_assignment_counts.sum()
        
        num_nurses = len(self.state.main_assignment_counts)
        avg_main = mains_sum / num_nurses
        avg_backup = backs_sum / num_nurses
        
        min_main = self.state.main_assignment_counts.min()
        min_backup = self.state.backup_assignment_counts.min()
        
        return (
            min_main > 0 and
            min_backup > 0 and
            min_main >= avg_main - tolerance and
            min_backup >= avg_backup - tolerance
        )

    def _try_week_permutations_no_revert(self, week_days: list[pd.Timestamp]) -> bool:
        """
        Try all permutations of modifiable ``(date, role)`` slots for one week and
        keep lexicographic improvements.
        """

        if not week_days:
            return False

        week_start = min(week_days)
        offset = (FRIDAY_WEEKDAY - week_start.weekday()) % 7
        friday = week_start + timedelta(days=offset)
        friday_label = friday.date()

        slots = [
            (date, role)
            for date in week_days
            for role in ("main", "backup")
            if not self._is_pre_scheduled(date, role)
        ]
        if not slots:
            self._debug_print(
                f"[ScheduleVariant] [WeekPerms] friday={friday_label} no-slots"
            )
            return False
        slot_tuple = tuple(slots)

        self._debug_print(
            f"[ScheduleVariant] [WeekPerms] start friday={friday_label} total_slots={len(slot_tuple)}"
        )

        original_state = self.backup_week_assignments(week_days)
        orig_tuple = self._spread_components()

        def run_attempt(force_relaxed: bool) -> bool:
            mode = "relaxed" if force_relaxed else "strict"
            self._restore_from_backup(week_days, original_state)
            self._clear_week_assignments(week_days)
            cleared_state = self.backup_week_assignments(week_days)

            best_state: WeekBackup | None = None
            best_tuple = orig_tuple
            improved = False

            perm_iterator = permutations(slot_tuple) if slot_tuple else [tuple()]

            for idx, order in enumerate(perm_iterator, start=1):
                if idx == 1 or idx % 50 == 0:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} perm={idx} mode={mode}"
                    )

                self._restore_from_backup(week_days, cleared_state)

                if not self._assign_slot_sequence(order, force_relaxed=force_relaxed):
                    continue

                new_tuple = self._spread_components()
                if self._lexi_better(new_tuple, best_tuple):
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} improved {best_tuple}->{new_tuple} mode={mode}"
                    )
                    best_tuple = new_tuple
                    best_state = self.backup_week_assignments(week_days)
                    improved = True

                if improved and best_tuple[0] <= 1 and best_tuple[1] <= 1:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} early-stop perm={idx} mode={mode}"
                    )
                    break

                if improved:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} improvement found perm={idx} mode={mode}"
                    )
                    break

            if improved and best_state is not None:
                self._restore_from_backup(week_days, best_state)
                self._debug_print(
                    f"[ScheduleVariant] [WeekPerms] friday={friday_label} applied mode={mode}"
                )
                return True

            self._restore_from_backup(week_days, original_state)
            return False

        if run_attempt(force_relaxed=False):
            return True

        if self.config.allow_one_day_weekday_gap and run_attempt(force_relaxed=True):
            return True

        self._debug_print(
            f"[ScheduleVariant] [WeekPerms] friday={friday_label} no-change"
        )
        return False

    def _clear_week_assignments(self, week_days: List[pd.Timestamp]) -> None:
        """Clear modifiable assignments for a week."""
        for d in week_days:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                nurse = self.state.schedule.at[d, role]
                if not is_empty(nurse):
                    self._dec_assign(d, role, nurse)

    def _try_week_assignment(self, perm: tuple[pd.Timestamp, ...]) -> bool:
        """
        Try to assign roles for a week permutation with dynamic per-slot sorting.
        """
        if not self.assign_nurses_to_weekdays(list(perm), 'main'):
            return False
        if not self.assign_nurses_to_weekdays(list(perm), 'backup'):
            return False
        return True

    def _restore_week_assignments(
        self, 
        week_days: List[pd.Timestamp], 
        original_assignments: pd.DataFrame
    ) -> None:
        """Restore original week assignments."""
        for d in week_days:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                self.state.schedule.at[d, role] = original_assignments.at[d, role]
        self._invalidate_weekday_cache()
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    # ===== GAP FILLING =====
    
    def iterative_gap_fill_no_revert(
        self,
        max_iterations: int = 40,
        tracker: Optional[BestStateTracker] = None,
    ) -> bool:
        """Iteratively fill weekday gaps using tracker-backed snapshots."""

        created_tracker = tracker is None
        if created_tracker:
            tracker = BestStateTracker(self)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.begin_iteration("[GapFill]")

        improved = False

        for pass_idx in range(1, max_iterations + 1):
            pass_quality = tracker.begin_iteration(f"[GapFill] Pass {pass_idx}")
            schedule_changed = False
            self._debug_print(
                f"[ScheduleVariant] [GapFill] pass={pass_idx} start total={pass_quality.total_gaps}"
            )

            for week_days in self.get_weeks():
                if not week_days:
                    continue
                week_label = f"{min(week_days).date()}-{max(week_days).date()}"
                week_gaps = self._count_week_gaps(week_days)
                self._debug_print(
                    f"[ScheduleVariant] [GapFill] pass={pass_idx} week={week_label} before={week_gaps}"
                )
                if week_gaps == 0:
                    continue

                remaining = self._try_week_gap_permutations_no_revert(week_days)
                if remaining < week_gaps:
                    schedule_changed = True
                self._debug_print(
                    f"[ScheduleVariant] [GapFill] pass={pass_idx} week={week_label} after={remaining}"
                )

            comparison = tracker.evaluate_and_commit(
                phase_name=f"[GapFill] Pass {pass_idx}",
                allow_neutral=False,
            )
            if comparison == Comparison.BETTER:
                improved = True

            current_gaps = self.count_gaps()
            self._debug_print(
                f"[ScheduleVariant] [GapFill] pass={pass_idx} end total={current_gaps}"
            )

            if current_gaps == 0:
                self._debug_print(f"[ScheduleVariant] [GapFill] complete pass={pass_idx}")
                tracker.restore_global_best()
                return True

            if comparison == Comparison.WORSE and not schedule_changed:
                self._debug_print(f"[ScheduleVariant] [GapFill] stalled pass={pass_idx}")
                break

        tracker.restore_global_best()
        final_quality = tracker.get_global_best_quality()

        if final_quality and initial_quality:
            improved = final_quality.total_gaps < initial_quality.total_gaps or improved

        print(
            f"[GapFill] Final: {initial_quality} -> {final_quality} "
            f"(improved={bool(improved)})"
        )

        return bool(final_quality and final_quality.total_gaps == 0)

    def _count_week_gaps(self, week_days: List[pd.Timestamp]) -> int:
        """Count gaps in a specific week."""
        return sum(
            is_empty(self.state.schedule.at[d, r])
            for d in week_days
            for r in ("main", "backup")
        )

    def _try_week_gap_permutations_no_revert(
        self,
        week_days: List[pd.Timestamp]
    ) -> int:
        """Fill gaps for one week using heuristic search and return remaining gaps."""

        if not week_days:
            return 0

        ordered_days = sorted(week_days)
        week_start = ordered_days[0]
        offset = (FRIDAY_WEEKDAY - week_start.weekday()) % 7
        friday = week_start + timedelta(days=offset)
        friday_label = friday.date()
        original_state = self.backup_week_assignments(ordered_days)
        orig_gaps = self._count_week_gaps(ordered_days)
        self._debug_print(
            f"[ScheduleVariant] [GapPerms] start friday={friday_label} gaps={orig_gaps}"
        )

        if orig_gaps == 0:
            return 0

        # Clear modifiable cells so the search operates on a clean slate
        self._clear_week_assignments(ordered_days)

        self._debug_print(
            f"[ScheduleVariant] [GapPerms] friday={friday_label} perm=1"
        )
        new_gaps = self._fill_week_with_permutation(ordered_days)

        if new_gaps < orig_gaps:
            # Counts already maintained by _inc_assign in _fill_week_with_permutation
            self._debug_print(
                f"[ScheduleVariant] [GapPerms] friday={friday_label} improved {orig_gaps}->{new_gaps}"
            )
            return new_gaps

        # Retry with forced relaxed domains if allowed and first attempt stalled
        if self.config.allow_one_day_weekday_gap:
            self._restore_from_backup(ordered_days, original_state)
            self._clear_week_assignments(ordered_days)
            self._debug_print(
                f"[ScheduleVariant] [GapPerms] friday={friday_label} perm=relaxed"
            )
            relaxed_gaps = self._fill_week_with_permutation(
                ordered_days,
                force_relaxed=True,
            )
            if relaxed_gaps < orig_gaps:
                # Counts already maintained by _inc_assign in _fill_week_with_permutation
                self._debug_print(
                    f"[ScheduleVariant] [GapPerms] friday={friday_label} relaxed-improved {orig_gaps}->{relaxed_gaps}"
                )
                return relaxed_gaps

        self._restore_from_backup(ordered_days, original_state)
        self._debug_print(
            f"[ScheduleVariant] [GapPerms] friday={friday_label} no-change"
        )
        return orig_gaps

    def _restore_from_backup(
        self, 
        week_days: List[pd.Timestamp], 
        state: 'WeekBackup'
    ) -> None:
        """Restore week state from backup."""
        sched = self.state.schedule
        sched.loc[week_days, ["main", "backup"]] = state.rows
        self.state.main_assignment_counts = state.main_counts.copy()
        self.state.backup_assignment_counts = state.backup_counts.copy()
        self.state.last_assignment = dict(state.last_assignment)
        self._invalidate_weekday_cache()

    def _fill_week_with_permutation(
        self,
        week_days: List[pd.Timestamp],
        *,
        force_relaxed: bool = False,
    ) -> int:
        """Assign one week's empty slots using backtracking and heuristic ordering."""

        if not week_days:
            self._debug_print("[ScheduleVariant] [WeekPerm] empty-week")
            return 0

        slots = self._build_window_varlist(week_days)
        if not slots:
            self._debug_print(
                f"[ScheduleVariant] [WeekPerm] no-slots {min(week_days).date()}-{max(week_days).date()}"
            )
            return self._count_week_gaps(week_days)

        window_label = f"{min(week_days).date()}-{max(week_days).date()}"
        self._debug_print(
            f"[ScheduleVariant] [WeekPerm] start {window_label} slots={len(slots)}"
        )

        cleared_state = self.backup_week_assignments(week_days)
        best_state: WeekBackup | None = None
        best_remaining = float("inf")

        slot_tuple = tuple(slots)
        perm_iterator = permutations(slot_tuple) if slot_tuple else [tuple()]

        for idx, order in enumerate(perm_iterator, start=1):
            if idx == 1 or idx % 50 == 0:
                self._debug_print(
                    f"[ScheduleVariant] [WeekPerm] permutation={idx} window={window_label}"
                )

            self._restore_from_backup(week_days, cleared_state)

            self._assign_slot_sequence(
                order,
                gap_mode=True,
                update_state=False,
                allow_partial=True,
                force_relaxed=force_relaxed,
            )

            remaining = self._count_week_gaps(week_days)
            if remaining < best_remaining:
                best_remaining = remaining
                best_state = self.backup_week_assignments(week_days)
                if best_remaining == 0:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerm] perfect assignment window={window_label} perm={idx}"
                    )
                    break

        if best_state is not None:
            self._restore_from_backup(week_days, best_state)
            remaining = best_remaining if best_remaining != float("inf") else self._count_week_gaps(week_days)
        else:
            self._restore_from_backup(week_days, cleared_state)
            remaining = self._count_week_gaps(week_days)

        self._debug_print(
            f"[ScheduleVariant] [WeekPerm] exhaustive result {window_label} gaps={remaining}"
        )
        return remaining
    # ===== DEBUG METHODS =====
    
    def print_current_schedule_state(self) -> None:
        """Debug method to print current schedule state."""
        print("\n==== CURRENT SCHEDULE STATE ====")
        print(self.state.schedule[['main', 'backup']].to_string())
        
        print("\nAssignment Counts:")
        print(f"Main: {dict(self.state.main_assignment_counts)}")
        print(f"Backup: {dict(self.state.backup_assignment_counts)}")
        
        print("\nLast Assignment Dates:")
        last_assign_dict = {
            k: (v.date() if v is not None else None) 
            for k, v in self.state.last_assignment.items()
        }
        print(last_assign_dict)
        
        print("\nLast Patterns:")
        print(self.state.last_pattern)
        
        print("\nWeekend Tracking:")
        weekend_tracking_dict = {
            k.date() if isinstance(k, pd.Timestamp) else k: v 
            for k, v in self.state.weekend_tracking.items()
        }
        print(weekend_tracking_dict)
        print("=" * 50)

    
_DEBUG = bool(int(os.getenv("DEBUG_SCHED", "1")))
_LOCK  = threading.Lock()        # atomic writes even from many processes


class AssignmentDebugLogger:
    """Structured assignment logger that writes JSONL and CSV side by side."""

    CSV_FIELDS = [
        "timestamp",
        "context",
        "phase",
        "date",
        "role",
        "final_pick",
        "eligible",
        "candidate_stats",
        "rejections",
        "counts_main",
        "counts_backup",
        "counts_total",
        "history_main",
        "history_backup",
        "note",
        "extra",
    ]

    def __init__(self, enabled: bool, *, directory: pathlib.Path | None = None) -> None:
        self.enabled = bool(enabled)
        self._json_handle: Optional[Any] = None
        self._csv_handle: Optional[Any] = None
        self._csv_writer: Optional[csv.DictWriter] = None
        self.json_path: Optional[pathlib.Path] = None
        self.csv_path: Optional[pathlib.Path] = None

        if not self.enabled:
            return

        base_dir = pathlib.Path(directory) if directory else pathlib.Path.cwd()
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_name = f"assignment_debug_{timestamp}"

        self.json_path = base_dir / f"{base_name}.jsonl"
        self.csv_path = base_dir / f"{base_name}.csv"

        self._json_handle = _open_dbg(str(self.json_path), "a")
        self._csv_handle = _open_dbg(str(self.csv_path), "a")

        if not self._json_handle or not self._csv_handle:
            # Could not open one or both handles (likely due to Windows file
            # locking when workers fork). Disable structured logging so the
            # scheduler continues instead of hanging forever.
            self.enabled = False
            self.close()
            return

        self._csv_writer = csv.DictWriter(self._csv_handle, fieldnames=self.CSV_FIELDS)
        if self._csv_handle.tell() == 0:
            self._csv_writer.writeheader()

        atexit.register(self.close)

    def close(self) -> None:
        """Close both debug files."""
        if self._json_handle:
            try:
                self._json_handle.close()
            finally:
                self._json_handle = None
        if self._csv_handle:
            try:
                self._csv_handle.close()
            finally:
                self._csv_handle = None
                self._csv_writer = None

    @staticmethod
    def _stringify(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (str, int, float)):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        return json.dumps(value, default=str, sort_keys=True)

    def log(self, payload: dict) -> None:
        """Write a payload to JSONL/CSV if debugging is enabled."""
        if not self.enabled or not payload:
            return

        record = payload.copy()
        record.setdefault("timestamp", datetime.datetime.now().isoformat())

        with _LOCK:
            assert self._json_handle is not None and self._csv_writer is not None and self._csv_handle is not None
            self._json_handle.write(json.dumps(record, default=str) + "\n")

            row = {field: self._stringify(record.get(field)) for field in self.CSV_FIELDS}
            self._csv_writer.writerow(row)
            self._json_handle.flush()
            self._csv_handle.flush()


ASSIGNMENT_DEBUG_LOGGER = AssignmentDebugLogger(enabled=_DEBUG)
_LOG_FILE_CACHE: Dict[str, str] = {}


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def log(kind: str, payload: dict):
    """Write one JSON line to <kind>_dump_<timestamp>.log."""
    if not _DEBUG:
        return
    line  = json.dumps(payload, default=str)
    with _LOCK:
        fname = _LOG_FILE_CACHE.setdefault(kind, f"{kind}_dump_{_ts()}.log")
        with open(fname, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            
class NurseScheduler:
    """
    Advanced nurse scheduling system with weekend rotation management, 
    constraint satisfaction, and multi-criteria optimization.
    """
    
    # Class constants
    WEEKDAYS = ['Friday', 'Saturday', 'Sunday']
    WEEKEND_DAYS_COUNT = 3
    FRIDAY_WEEKDAY = 4
    
    # PDF layout constants
    PDF_FONT_SIZES = {
        'title': 32,
        'dow': 16,
        'dayno': 14,
        'name': 18
    }
    
    def __init__(self,
                 start_date,
                 end_date,
                 nurses: list,
                 prn_nurses: list,
                 nurse_manager,
                 weekend_history,
                 pre_scheduler,
                 config: SchedulerConfig | None = None,
                 history_window_days: int = 30):
        
        # Core date and personnel setup
        self._initialize_core_attributes(
            start_date, end_date, nurses, prn_nurses, 
            nurse_manager, weekend_history, pre_scheduler, config
        )
        
        # Initialize scheduling data structures
        self._initialize_scheduling_data()
        
        # Setup historical data and tracking
        self.history_window_days = history_window_days
        self._initialize_historical_data()
        
        # Initialize nurse assignment tracking
        self._initialize_nurse_tracking()
        
        # Configuration for rotation violations
        self.nurses_allowed_rotation_violation: set[str] = set()

    def _initialize_core_attributes(self, start_date, end_date, nurses, prn_nurses,
                                   nurse_manager, weekend_history, pre_scheduler, config):
        """Initialize the core attributes of the scheduler (now with deterministic nurse order)."""
        self.start_date = DateUtils.normalize_date(start_date)
        self.end_date   = DateUtils.normalize_date(end_date)
    
        # Deterministic ordering across processes/runs
        self.nurses      = sorted(list(nurses), key=str.casefold)
        self.prn_nurses  = sorted(list(prn_nurses), key=str.casefold)
    
        self.nurse_manager   = nurse_manager
        self.weekend_history = weekend_history
        self.pre_scheduler   = pre_scheduler
        self.config          = config if config is not None else SchedulerConfig()
    
    def _initialize_scheduling_data(self):
        """Initialize the main scheduling data structures."""
        self.schedule = self._initialize_schedule()
        self.availability = self._initialize_availability()
        self.main_assignment_counts = pd.Series(0, index=self.nurses)
        self.backup_assignment_counts = pd.Series(0, index=self.nurses)

    def _initialize_nurse_tracking(self):
        """Initialize tracking data for nurse assignments and patterns."""
        self.weekend_tracking = {}
        self.last_assignment = {}
        self.last_pattern = {}
        self.rotation_violation_history = defaultdict(list)
        self._rotation_violations = []

        for nurse in self.nurses:
            last_wk = self.weekend_history.get_last_weekend_before(nurse, self.start_date)
            self.last_assignment[nurse] = last_wk
            self.last_pattern[nurse] = self.weekend_history.get_last_pattern(nurse)

    @staticmethod
    def _as_friday(d: pd.Timestamp) -> pd.Timestamp:
        """Return the Friday of the Fri–Sun block containing d."""
        # Friday == 4 (same convention used across the module)
        offset = (d.weekday() - NurseScheduler.FRIDAY_WEEKDAY) % 7
        return d - pd.Timedelta(days=offset)

    def _initialize_historical_data(self):
        """Initialize historical assignment tracking."""
        try:
            self.assignment_history = AssignmentHistory(self.nurse_manager.db_name)
        except Exception:
            # DB is missing or table empty – proceed with empty history
            self.assignment_history = None

        self._historical_main, self._historical_backup = self._compute_historical_counts()

    # =====================================================================
    # HISTORICAL DATA METHODS
    # =====================================================================

    def _compute_historical_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """
        Return two dictionaries:
            • {nurse: main_count}
            • {nurse: backup_count}

        covering the previous `history_window_days` (default 30) that END
        the day *before* `self.start_date`.

        If the history table is empty or missing, we return 0-filled dicts
        so the scheduler continues without long-term logic.
        """
        empty = {n: 0 for n in self.nurses}

        if not getattr(self, "assignment_history", None):
            return empty.copy(), empty.copy()

        window_start = (self.start_date - timedelta(days=self.history_window_days)).normalize()
        window_end = (self.start_date - timedelta(days=1)).normalize()

        try:
            main, backup = self.assignment_history.get_counts(window_start, window_end)
        except Exception:
            # corrupt / absent table → behave as if no history
            return empty.copy(), empty.copy()

        # ensure every nurse key exists
        for n in self.nurses:
            main.setdefault(n, 0)
            backup.setdefault(n, 0)

        return main, backup

    def _historic_overage(self) -> dict[str, int]:
        """
        For every nurse, compute:
            (30-day total assignments) − (median of all totals)
        Positive values mean that nurse was *over-utilised* last month.
        """
        totals = {
            n: self._historical_main.get(n, 0) + self._historical_backup.get(n, 0)
            for n in self.nurses
        }
        if not totals:
            return {n: 0 for n in self.nurses}

        median_val = round(float(np.median(list(totals.values()))))
        return {n: totals[n] - median_val for n in self.nurses}

    # =====================================================================
    # SCORING AND EVALUATION METHODS
    # =====================================================================

    @staticmethod
    def _long_term_score(nurse_counts: dict[str, dict[str, int]],
                         overage: dict[str, int]) -> int:
        """
        Penalty used when sorting variants.
        For each nurse:
            extra = max(0, overage[n] + variant_total - min_total)
        We sum those extras.  Lower score = variant that helps previously
        over-used nurses land at or below the minimum total in this run.
        """
        if not nurse_counts:
            return 0

        min_total = min(c["total"] for c in nurse_counts.values())

        penalty = 0
        for n, counts in nurse_counts.items():
            penalty += max(0, overage.get(n, 0) + counts["total"] - min_total)
        return penalty

    # --- Add inside NurseScheduler ---------------------------------------------

    def _fridays_in_range(self, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
        """All Fridays in [start, end]."""
        start = DateUtils.normalize_date(start)
        end = DateUtils.normalize_date(end)
        # jump to first Friday on/after start
        offset = (self.FRIDAY_WEEKDAY - start.weekday()) % 7
        cur = start + timedelta(days=offset)
        out: list[pd.Timestamp] = []
        while cur <= end:
            out.append(cur)
            cur += timedelta(days=7)
        return out
    
    def _as_friday(self, dt: Optional[pd.Timestamp]) -> Optional[pd.Timestamp]:
        """Map any date within a weekend to its Friday (or return None)."""
        if dt is None:
            return None
        return dt - timedelta(days=(dt.weekday() - self.FRIDAY_WEEKDAY) % 7)
    
    def _rotation_violation_score(self, nurse_counts: dict[str, dict[str, int]],
                                 historic_viol: dict[str, int],
                                 sched_df: pd.DataFrame) -> int:
        """
        Candidate-sensitive additive penalty.

        Historic repeat counts are weighted by how often a nurse appears in the
        candidate's weekend rows (Fri/Sat/Sun). This differentiates variants
        that use high-violation nurses more heavily on weekends.
        """
        if sched_df is None or sched_df.empty:
            return 0

        weekend_rows = sched_df.loc[sched_df.index.weekday.isin([4, 5, 6]), ["main", "backup"]]
        if weekend_rows.empty:
            return 0

        weekend_appearances: dict[str, int] = defaultdict(int)
        for _, roles in weekend_rows.iterrows():
            for nurse in roles.tolist():
                if is_empty(nurse):
                    continue
                weekend_appearances[str(nurse)] += 1

        # Keep scope to nurses represented in this candidate's counts.
        return sum(
            historic_viol.get(nurse, 0) * weekend_appearances.get(nurse, 0)
            for nurse in nurse_counts
        )

    # ────────────────────────────────────────────────────────────────────
    # NurseScheduler._weekend_gap_penalty
    # ────────────────────────────────────────────────────────────────────
    def _weekend_gap_penalty(self, schedule_df: pd.DataFrame) -> int:
        """Penalty based on *all* consecutive Friday-to-Friday gaps per nurse.

        We build each nurse's timeline by merging:
          • historic weekends before ``start_date`` (from ``weekend_history``),
          • every Friday in ``schedule_df`` where the nurse is assigned, and
          • a forward-looking horizon Friday after ``end_date`` to recognise
            nurses who are not scheduled again within the window.

        Consecutive gaps are measured only when the later Friday falls inside
        or beyond the candidate schedule (``>= start_date``).  The resulting
        penalty prefers variants that maximise those gaps while keeping them
        balanced – reassigning a recently worked nurse later in the period now
        incurs a tangible cost.
        """

        # Collect every Friday assignment inside the candidate schedule
        friday_rows = schedule_df.loc[
            schedule_df.index.weekday == self.FRIDAY_WEEKDAY, ["main", "backup"]
        ]

        weekend_assignments: dict[str, list[pd.Timestamp]] = defaultdict(list)
        for friday, roles in friday_rows.iterrows():
            seen: set[str] = set()
            for nurse in roles.tolist():
                if is_empty(nurse):
                    continue
                if nurse in seen:
                    continue
                seen.add(nurse)
                weekend_assignments.setdefault(nurse, []).append(self._as_friday(friday))

        # Ensure we examine every nurse we actively schedule
        all_nurses: set[str] = set(self.nurses) | set(weekend_assignments.keys())

        # Forward horizon (first Friday on/after end_date + gap window)
        horizon_base = self.end_date + timedelta(days=self.config.weekend_gap_days)
        horizon_offset = (self.FRIDAY_WEEKDAY - horizon_base.weekday()) % 7
        horizon_friday = horizon_base + timedelta(days=horizon_offset)

        target_gap = int(self.config.weekend_gap_days)
        all_gaps: list[int] = []

        for nurse in sorted(all_nurses):
            history_fridays = [
                self._as_friday(DateUtils.normalize_date(hist))
                for hist in self.weekend_history.get_weekends(nurse)
            ]
            history_fridays = [d for d in history_fridays if d is not None and d < self.start_date]
            history_fridays = sorted(set(history_fridays))

            if not history_fridays:
                synthetic_prev = self._as_friday(
                    self.start_date - timedelta(days=target_gap + 1)
                )
                history_fridays = [synthetic_prev]

            scheduled_fridays = sorted(set(weekend_assignments.get(nurse, [])))

            timeline = sorted(set(history_fridays + scheduled_fridays))
            if not timeline:
                timeline = history_fridays[:]

            if not timeline:
                # Should not happen, but keep defensive guard
                continue

            if timeline[-1] < horizon_friday:
                timeline.append(horizon_friday)

            nurse_gaps = []
            for prev, curr in zip(timeline, timeline[1:]):
                if curr < self.start_date:
                    continue
                gap_days = (curr - prev).days
                if gap_days <= 0:
                    continue
                nurse_gaps.append(gap_days)
                all_gaps.append(gap_days)

            if not nurse_gaps:
                # No future assignments → still account for horizon bridge
                gap_days = (timeline[-1] - timeline[-2]).days if len(timeline) >= 2 else 0
                if gap_days > 0:
                    all_gaps.append(gap_days)

        if not all_gaps:
            return 0

        min_gap = min(all_gaps)
        max_gap = max(all_gaps)
        spread = max_gap - min_gap
        deficit = sum(max(0, target_gap - gap) for gap in all_gaps)

        return int(spread + deficit)
    
    # =====================================================================
    # SCHEDULE INITIALIZATION METHODS
    # =====================================================================

    def _initialize_schedule(self) -> pd.DataFrame:
        """Initialize the main schedule DataFrame with basic structure."""
        dates = pd.date_range(self.start_date, self.end_date, freq='D')
        schedule = pd.DataFrame(index=dates, columns=['main', 'backup'])
        schedule['main'] = None
        schedule['backup'] = None
        schedule['day_of_week'] = schedule.index.day_name()
        schedule['is_weekend'] = schedule['day_of_week'].isin(self.WEEKDAYS)
        return schedule

    def _initialize_availability(self) -> pd.DataFrame:
        """Initialize nurse availability matrix."""
        availability = pd.DataFrame(True, index=self.schedule.index, columns=self.nurses)
        
        for nurse in self.nurses:
            unavailable_dates = self.nurse_manager.get_unavailable_dates(nurse)
            unavailable_dates = pd.to_datetime(list(unavailable_dates))
            valid_dates = set(unavailable_dates).intersection(set(availability.index))
            availability.loc[list(valid_dates), nurse] = False
            
        return availability

    # =====================================================================
    # PRE-SCHEDULING METHODS
    # =====================================================================

    def _collect_pre_scheduled_slots(self) -> Dict[pd.Timestamp, Dict[str, str]]:
        """
        Return {date → {"main": name_or_None, "backup": name_or_None}}
        for every pre-scheduled row inside the requested period.
        """
        raw = self.pre_scheduler.get_assignments_in_range(self.start_date, self.end_date)
        slots: Dict[pd.Timestamp, Dict[str, str]] = {}
        
        for day_str, row in raw.items():
            ts = DateUtils.normalize_date(day_str)
            slots[ts] = {
                "main": row.get("main"),
                "backup": row.get("backup")
            }
        return slots

    # --- Replace in NurseScheduler ---------------------------------------------

    def _get_pre_scheduled_weekend_assignments(self) -> dict:
        """
        Extract pre-scheduled weekend assignments for:
          • the schedule window, and
          • an additional forward horizon = weekend_gap_days (+2 for Sat/Sun)
        so forward-gap checks can block end-of-window conflicts.
        """
        # extend scan to capture the first full weekend after gap_min
        horizon_end = (self.end_date + timedelta(days=self.config.weekend_gap_days + 2)).normalize()
    
        # pull pre-scheduled rows across the extended range
        pre_scheduled = self.pre_scheduler.get_assignments_in_range(self.start_date, horizon_end)
        pre_scheduled_df = pd.DataFrame.from_dict(pre_scheduled, orient='index')
        if not pre_scheduled_df.empty:
            pre_scheduled_df.index = pd.DatetimeIndex(
                [DateUtils.normalize_date(idx) for idx in pre_scheduled_df.index]
            )
    
        # build Friday keys for both the in-window weekends and the forward horizon
        fridays = self._fridays_in_range(self.start_date, horizon_end)
        weekend_assignments = {
            friday: {WeekendPattern.FSF: None, WeekendPattern.SFS: None}
            for friday in fridays
        }
    
        # direct Friday hints (Fri main → FSF, Fri backup → SFS)
        if not pre_scheduled_df.empty:
            friday_rows = pre_scheduled_df.loc[pre_scheduled_df.index.weekday == self.FRIDAY_WEEKDAY]
            for friday in fridays:
                if friday in friday_rows.index:
                    fri_main = friday_rows.at[friday, 'main'] if 'main' in friday_rows.columns else None
                    fri_backup = friday_rows.at[friday, 'backup'] if 'backup' in friday_rows.columns else None
                    if fri_main and not self.is_empty(fri_main):
                        weekend_assignments[friday][WeekendPattern.FSF] = fri_main
                    if fri_backup and not self.is_empty(fri_backup):
                        weekend_assignments[friday][WeekendPattern.SFS] = fri_backup
    
            # infer FSF/SFS when whole-weekend is prefilled but Friday wasn't explicit
            self._infer_weekend_patterns_from_whole_weekend(
                weekend_assignments, pre_scheduled_df, fridays
            )
    
        return weekend_assignments
        
    def _infer_weekend_patterns_from_whole_weekend(self, weekend_assignments, 
                                                  pre_scheduled_df, fridays):
        """Infer FSF/SFS patterns from whole weekend assignments when not explicitly set."""
        for friday in fridays:
            if (self.is_empty(weekend_assignments[friday][WeekendPattern.FSF]) and 
                self.is_empty(weekend_assignments[friday][WeekendPattern.SFS])):
                
                weekend_dates = [
                    friday, 
                    friday + pd.Timedelta(days=1), 
                    friday + pd.Timedelta(days=2)
                ]
                weekend_df = pre_scheduled_df.loc[pre_scheduled_df.index.isin(weekend_dates)]
                nurse_counts = {}
                
                for _, row in weekend_df.iterrows():
                    self._count_nurse_assignments(row, nurse_counts)
                
                # Only assign if a nurse is not assigned to both roles
                for nurse, counts in nurse_counts.items():
                    if (counts["main"] + counts["backup"] >= 2 and 
                        counts["main"] != counts["backup"]):
                        pattern = (WeekendPattern.FSF if counts["main"] > counts["backup"] 
                                 else WeekendPattern.SFS)
                        weekend_assignments[friday][pattern] = nurse

    def _count_nurse_assignments(self, row, nurse_counts):
        """Count main and backup assignments for nurses in a given row."""
        main_nurse = row.get('main')
        backup_nurse = row.get('backup')
        
        if main_nurse and not self.is_empty(main_nurse):
            nurse_counts.setdefault(main_nurse, {"main": 0, "backup": 0})["main"] += 1
        if backup_nurse and not self.is_empty(backup_nurse):
            nurse_counts.setdefault(backup_nurse, {"main": 0, "backup": 0})["backup"] += 1

    # =====================================================================
    # WEEKEND MANAGEMENT METHODS
    # =====================================================================

    def _get_weekends(self) -> list[pd.Timestamp]:
        """
        Return every Friday for which the *whole* Fri-Sat-Sun block lies
        inside [start_date … end_date] (i.e. friday + 2 ≤ end_date).
        """
        weekends: list[pd.Timestamp] = []

        # latest Friday that can still form a complete weekend
        last_allowed_fri = (self.end_date - timedelta(days=2)).normalize()

        current = self.start_date.normalize()
        while current <= last_allowed_fri:
            if current.weekday() == self.FRIDAY_WEEKDAY:  # Friday
                weekends.append(current)
            current += timedelta(days=1)

        return weekends

    def _weekend_dates(self, friday: pd.Timestamp) -> List[pd.Timestamp]:
        """Return the three dates of a weekend given its Friday."""
        return [friday, friday + timedelta(days=1), friday + timedelta(days=2)]

    # --- Replace in NurseScheduler ---------------------------------------------

    def _get_next_weekend_assignment(self, nurse, current_weekend, schedule,
                                     all_pre_scheduled_weekends=None):
        """
        Earliest *future* weekend (as its Friday) on which nurse is assigned,
        considering both the current schedule and all pre-scheduled weekends
        (which may extend beyond the schedule window).
        """
        # From the current schedule (within window)
        weekend_mask = schedule['day_of_week'].isin(self.WEEKDAYS)
        weekend_df = schedule.loc[weekend_mask]
        cut_off = current_weekend + timedelta(days=2)  # after this Sunday
        future_weekends = weekend_df.loc[weekend_df.index > cut_off]
        assigned = future_weekends[
            (future_weekends['main'] == nurse) | (future_weekends['backup'] == nurse)
        ]
        next_in_schedule = assigned.index.min() if not assigned.empty else None
        next_in_schedule = self._as_friday(next_in_schedule)
    
        # From pre-scheduled weekends (can be beyond window)
        next_in_pre = self._find_next_pre_scheduled_weekend(
            nurse, current_weekend, all_pre_scheduled_weekends
        )
        next_in_pre = self._as_friday(next_in_pre)
    
        candidates = [d for d in (next_in_schedule, next_in_pre) if d is not None]
        return min(candidates) if candidates else None
        
    def _find_next_pre_scheduled_weekend(self, nurse, current_weekend, 
                                       all_pre_scheduled_weekends):
        """Find the next pre-scheduled weekend for a nurse."""
        next_in_pre = None
        if all_pre_scheduled_weekends:
            for wk, roles in all_pre_scheduled_weekends.items():
                if wk > current_weekend and nurse in roles.values():
                    if next_in_pre is None or wk < next_in_pre:
                        next_in_pre = wk
        return next_in_pre

    # =====================================================================
    # NURSE VALIDATION METHODS
    # =====================================================================

    def _is_nurse_available_for_weekend(self, nurse: str, weekend: pd.Timestamp) -> bool:
        """Check if a nurse is available for all three days of a weekend."""
        if self.nurse_manager.is_prn_nurse(nurse):
            return False
    
        weekend_dates = self._weekend_dates(weekend)
        weekend_dates_in_idx = [d for d in weekend_dates if d in self.availability.index]
        if len(weekend_dates_in_idx) != self.WEEKEND_DAYS_COUNT:
            return False
    
        try:
            vals = self.availability.loc[weekend_dates_in_idx, nurse]
        except KeyError:
            return False
    
        return bool(vals.apply(lambda v: (not pd.isna(v)) and bool(v)).all())
        
    def _check_weekend_gap_constraints(
        self,
        nurse: str,
        weekend: pd.Timestamp,
        last_assignment: dict,  # kept for signature compatibility; not used
        schedule: pd.DataFrame,
        all_pre_scheduled_weekends: dict,
    ) -> bool:
        """
        Hard rule: a nurse must have strictly more than config.weekend_gap_days
        between Fridays of two worked weekends — both backward and forward.
    
        This uses weekend history and the current schedule (including any
        pre-scheduled weekends) to decide.
        """
        gap_min = self.config.weekend_gap_days
    
        # ── backward gap: use historic last and any prior worked weekend in this schedule ──
        prev_wk_hist = self.weekend_history.get_last_weekend_before(nurse, weekend)
    
        prev_wk_sched = None
        prior_fridays = [
            d for d in schedule.index
            if d.weekday() == self.FRIDAY_WEEKDAY and d < weekend
        ]
        for f in reversed(prior_fridays):
            # Check Fri/Sat/Sun block for this nurse
            block = [f, f + pd.Timedelta(days=1), f + pd.Timedelta(days=2)]
            found = False
            for day in block:
                if day in schedule.index:
                    if (schedule.at[day, "main"] == nurse) or (schedule.at[day, "backup"] == nurse):
                        prev_wk_sched = f
                        found = True
                        break
            if found:
                break
    
        # Choose the latest of the two candidates
        if prev_wk_hist is not None and prev_wk_sched is not None:
            prev_wk = max(prev_wk_hist, prev_wk_sched)
        else:
            prev_wk = prev_wk_hist or prev_wk_sched
    
        if prev_wk is not None:
            # Compare Friday→Friday
            if (weekend - prev_wk).days <= gap_min:
                return False
    
        # ── forward gap: first future worked weekend from schedule or pre-scheduled ──
        next_wk = self._get_next_weekend_assignment(
            nurse, weekend, schedule, all_pre_scheduled_weekends
        )
        if next_wk is not None:
            # Ensure we compare Friday→Friday regardless of which day was assigned
            next_friday = self._as_friday(next_wk)
            if (next_friday - weekend).days <= gap_min:
                return False
    
        return True
        
    def _check_rotation_constraints(self, nurse: str, last_pattern: dict, 
                                  enforce_rotation: bool,
                                  nurses_allowed_rotation_violation: set[str]) -> tuple[bool, bool]:
        """
        Check rotation constraints for a nurse.
        Returns (can_do_fsf, can_do_sfs)
        """
        last_pat = last_pattern.get(nurse)
        if last_pat is None:
            return True, True  # No prior pattern → both patterns possible

        if enforce_rotation:
            # Strict: only the opposite pattern is allowed
            if last_pat == WeekendPattern.FSF:
                return False, True  # eligible SFS only
            if last_pat == WeekendPattern.SFS:
                return True, False  # eligible FSF only
        else:
            # Relaxed: may repeat only if nurse is whitelisted
            if nurse in nurses_allowed_rotation_violation:
                return True, True  # eligible both (violation allowed)
            else:
                if last_pat == WeekendPattern.FSF:
                    return False, True  # eligible SFS only
                if last_pat == WeekendPattern.SFS:
                    return True, False  # eligible FSF only

        return True, True

    def _is_nurse_valid_for_pattern(
        self,
        nurse: str,
        weekend: pd.Timestamp,
        variant,
        pattern: "WeekendPattern",
    ) -> bool:
        """
        Test whether basic hard rules allow `nurse` to work the given `pattern`
        on `weekend`.
    
        Rotation repetition is evaluated elsewhere; this only checks PRN,
        availability, and backward gap constraints.
        """
        # PRN staff never work weekends
        if self.nurse_manager.is_prn_nurse(nurse):
            return False
    
        # Must be available for all three days (NaN = unavailable)
        weekend_dates = self._weekend_dates(weekend)
        if not all(d in self.availability.index for d in weekend_dates):
            return False
    
        try:
            avail_ok = (
                self.availability.loc[weekend_dates, nurse]
                .map(lambda v: (not pd.isna(v)) and bool(v))
                .all()
            )
            if not avail_ok:
                return False
        except KeyError:
            return False
    
        # Backward gap: use nurse_weekend_lists (Fridays-only), not last_assignment
        prev_friday, _ = variant._get_neighboring_fridays(nurse, weekend)
        if prev_friday is not None:
            if (weekend - prev_friday).days <= self.config.weekend_gap_days:
                return False
    
        return True
        
    def _is_nurse_valid_for_fsf(self, nurse, weekend, variant):
        """Return True iff <nurse> can take the FSF pattern on <weekend>."""
        return self._is_nurse_valid_for_pattern(nurse, weekend, variant, WeekendPattern.FSF)

    def _is_nurse_valid_for_sfs(self, nurse, weekend, variant):
        """Return True iff <nurse> can take the SFS pattern on <weekend>."""
        return self._is_nurse_valid_for_pattern(nurse, weekend, variant, WeekendPattern.SFS)

    # =====================================================================
    # NURSE PAIR VALIDATION METHODS
    # =====================================================================

    def _get_valid_nurse_pairs(self, weekend: pd.Timestamp, last_assignment: dict,
                             last_pattern: dict, pre_scheduled: dict, weekend_tracking: dict, *,
                             schedule: pd.DataFrame | None = None,
                             all_pre_scheduled_weekends: dict | None = None,
                             enforce_rotation: bool = True,
                             nurses_allowed_rotation_violation: set[str] | None = None,
                             **_ignored_kwargs) -> list[tuple[str, str]]:
        """
        Return every (fsf_nurse, sfs_nurse) pair that satisfies every
        hard-rule (availability, gap, rotation, etc.).
        """
        # Normalize optional arguments
        schedule = schedule if schedule is not None else self.schedule
        nurses_allowed_rotation_violation = (
            set() if nurses_allowed_rotation_violation is None
            else set(nurses_allowed_rotation_violation)
        )

        # If we are in the relaxed branch and caller supplied empty set,
        # interpret as "every nurse may violate"
        if (not enforce_rotation) and (not nurses_allowed_rotation_violation):
            nurses_allowed_rotation_violation = set(self.nurses)

        # Get pre-scheduled names
        fsf_pre = pre_scheduled.get(WeekendPattern.FSF)
        sfs_pre = pre_scheduled.get(WeekendPattern.SFS)

        # Get valid nurses for each pattern
        valid_fsf, valid_sfs = self._get_valid_nurses_for_patterns(
            weekend, last_assignment, last_pattern, schedule,
            all_pre_scheduled_weekends, enforce_rotation,
            nurses_allowed_rotation_violation
        )

        # Force-include any fixed names that the filters might have pruned
        self._ensure_pre_scheduled_nurses_included(fsf_pre, sfs_pre, valid_fsf, valid_sfs)

        # Build pairs respecting fixed assignments
        pairs = self._build_nurse_pairs(fsf_pre, sfs_pre, valid_fsf, valid_sfs)

        # Reject pairs that conflict with any non-empty prefilled weekend cells.
        pairs = [
            (fsf, sfs)
            for fsf, sfs in pairs
            if self._pair_matches_prefilled_weekend_cells(schedule, weekend, fsf, sfs)
        ]

        # Filter out invalid late-shift combinations
        return self._filter_late_shift_pairs(pairs, fsf_pre, sfs_pre)

    def _pair_matches_prefilled_weekend_cells(
        self,
        schedule: pd.DataFrame,
        weekend: pd.Timestamp,
        fsf_nurse: str,
        sfs_nurse: str,
    ) -> bool:
        """
        Return True iff (fsf_nurse, sfs_nurse) is compatible with any existing
        non-empty Friday/Saturday/Sunday main/backup cells.
        """
        implied_assignments = (
            (weekend, "main", fsf_nurse),                      # Fri main  = FSF
            (weekend, "backup", sfs_nurse),                    # Fri backup= SFS
            (weekend + timedelta(days=1), "main", sfs_nurse),  # Sat main  = SFS
            (weekend + timedelta(days=1), "backup", fsf_nurse),# Sat backup= FSF
            (weekend + timedelta(days=2), "main", fsf_nurse),  # Sun main  = FSF
            (weekend + timedelta(days=2), "backup", sfs_nurse),# Sun backup= SFS
        )

        for day, role, expected_nurse in implied_assignments:
            if day not in schedule.index:
                continue
            prefilled_nurse = schedule.at[day, role]
            if self.is_empty(prefilled_nurse):
                continue
            if prefilled_nurse != expected_nurse:
                return False

        return True

    def _get_valid_nurses_for_patterns(self, weekend, last_assignment, last_pattern,
                                     schedule, all_pre_scheduled_weekends,
                                     enforce_rotation, nurses_allowed_rotation_violation):
        """Get lists of valid nurses for FSF and SFS patterns."""
        valid_fsf: list[str] = []
        valid_sfs: list[str] = []

        weekend_dates_in_idx = [d for d in self._weekend_dates(weekend)
                               if d in self.availability.index]

        _dbg_pairs("\n--- _get_valid_nurse_pairs ---")
        _dbg_pairs(f"Weekend: {weekend.date()}")

        for nurse in self.nurses:
            # Check basic eligibility
            if not self._is_nurse_eligible_for_weekend(
                nurse, weekend_dates_in_idx, last_assignment, weekend,
                schedule, all_pre_scheduled_weekends
            ):
                continue

            # Check rotation constraints
            can_fsf, can_sfs = self._check_rotation_constraints(
                nurse, last_pattern, enforce_rotation, nurses_allowed_rotation_violation
            )

            if can_fsf:
                valid_fsf.append(nurse)
            if can_sfs:
                valid_sfs.append(nurse)

        return valid_fsf, valid_sfs

    def _is_nurse_eligible_for_weekend(self, nurse, weekend_dates_in_idx, 
                                       last_assignment, weekend, schedule,
                                       all_pre_scheduled_weekends):
        """Check basic eligibility for weekend assignment (PRN, availability, gap)."""
        # PRN staff never work weekends
        if self.nurse_manager.is_prn_nurse(nurse):
            _reject(nurse, "PRN")
            return False
    
        # Must be available for all three days (NaN = unavailable)
        if len(weekend_dates_in_idx) != self.WEEKEND_DAYS_COUNT:
            _reject(nurse, "Unavailable for full weekend")
            return False
    
        try:
            vals = self.availability.loc[weekend_dates_in_idx, nurse]
        except KeyError:
            _reject(nurse, "Unavailable for full weekend")
            return False
    
        if not vals.apply(lambda v: (not pd.isna(v)) and bool(v)).all():
            _reject(nurse, "Unavailable for full weekend")
            return False
    
        # Weekend gap constraints (backward via history/schedule, forward via schedule/pre-scheduled)
        if not self._check_weekend_gap_constraints(
            nurse, weekend, last_assignment, schedule, all_pre_scheduled_weekends
        ):
            return False
    
        return True
    def _ensure_pre_scheduled_nurses_included(self, fsf_pre, sfs_pre, valid_fsf, valid_sfs):
        """Ensure pre-scheduled nurses are included in valid lists."""
        if fsf_pre and fsf_pre not in valid_fsf:
            valid_fsf.append(fsf_pre)
        if sfs_pre and sfs_pre not in valid_sfs:
            valid_sfs.append(sfs_pre)

    def _build_nurse_pairs(self, fsf_pre, sfs_pre, valid_fsf, valid_sfs):
        """Build nurse pairs respecting any fixed assignments."""
        if fsf_pre and sfs_pre:
            pairs = [(fsf_pre, sfs_pre)] if fsf_pre != sfs_pre else []
        elif fsf_pre:
            pairs = [(fsf_pre, s) for s in valid_sfs if s != fsf_pre]
        elif sfs_pre:
            pairs = [(f, sfs_pre) for f in valid_fsf if f != sfs_pre]
        else:
            pairs = [(f, s) for f in valid_fsf for s in valid_sfs if f != s]
        return pairs

    def _filter_late_shift_pairs(self, pairs, fsf_pre: Optional[str] = None, sfs_pre: Optional[str] = None):
        """Filter out late/late pairs unless the combination is fully pre-scheduled."""
        result: list[tuple[str, str]] = []
        allow_prescheduled_pair = bool(fsf_pre and sfs_pre)
        for f, s in pairs:
            both_late = (
                self.nurse_manager.is_late_shift_nurse(f)
                and self.nurse_manager.is_late_shift_nurse(s)
            )
            if both_late:
                if allow_prescheduled_pair and f == fsf_pre and s == sfs_pre:
                    _pair(f, s)
                    result.append((f, s))
                continue
            _pair(f, s)
            result.append((f, s))
        return result

    # =====================================================================
    # VARIANT GENERATION METHODS
    # =====================================================================

    def generate_all_weekend_variants(self, *, allow_rotation_violations: bool = False) -> list["ScheduleVariant"]:
        """
        Build every feasible schedule variant. Logs branching and state to debug_variants.txt.
        """
        if _DBG_FILE_VARIANTS:
            _DBG_FILE_VARIANTS.seek(0)
            _DBG_FILE_VARIANTS.truncate()
        _dbg_variants("=== generate_all_weekend_variants debug ===")

        self._rotation_violations = []
        self._rotation_enforced = True

        try:
            weekends = self._get_weekends()
            pre_weekend_assignments = self._get_pre_scheduled_weekend_assignments()
            pre_scheduled_slots = self._collect_pre_scheduled_slots()

            initial = ScheduleVariant(
                self.get_state_snapshot(),
                self.nurses,
                self.availability,
                self.config,
                self.nurse_manager,
                pre_scheduled_slots,
            )
            variants = [initial]

            for friday in weekends:
                variants = self._process_weekend_variants(
                    friday, variants, pre_weekend_assignments, allow_rotation_violations
                )
                if not variants:
                    _dbg_variants(f"  ERROR: no variants left after {friday.date()}")
                    return []

            _dbg_variants(f"\nFinal total variants: {len(variants)}")
            return variants

        except Exception:
            import traceback
            _dbg_variants("EXCEPTION:\n")
            _dbg_variants(traceback.format_exc())
            return []

    def _process_weekend_variants(self, friday, variants, pre_weekend_assignments, 
                                allow_rotation_violations):
        """Process variants for a specific weekend."""
        _dbg_variants(f"\n--- Weekend {friday.date()} ---")
        _dbg_variants(f"  starting variants count: {len(variants)}")
        
        for i, var in enumerate(variants):
            _dbg_variants(
                f"    VAR#{i} last_assign: {var.state.last_assignment}  "
                f"last_pat: {var.state.last_pattern}"
            )

        next_vars: list[ScheduleVariant] = []
        fixed = pre_weekend_assignments.get(friday, {})

        # Strict pass
        if not allow_rotation_violations:
            next_vars = self._generate_strict_variants(variants, friday, fixed, 
                                                     pre_weekend_assignments)
            _dbg_variants(f"  after strict pass: {len(next_vars)} variants")

        # Repeat-allowed pass
        if allow_rotation_violations or not next_vars:
            next_vars = self._generate_relaxed_variants(variants, friday, fixed,
                                                      pre_weekend_assignments, next_vars)
            _dbg_variants(f"  after repeat-allowed pass: {len(next_vars)} variants")

        return next_vars

    def _generate_strict_variants(self, variants, friday, fixed, pre_weekend_assignments):
        """Generate variants with strict rotation enforcement."""
        next_vars = []
        for var in variants:
            pairs = self._get_valid_nurse_pairs(
                friday, var.state.last_assignment, var.state.last_pattern,
                fixed, var.state.weekend_tracking,
                schedule=var.state.schedule,
                all_pre_scheduled_weekends=pre_weekend_assignments,
                enforce_rotation=True,
            )
            for fsf, sfs in pairs:
                clone = var.clone()
                clone.assign_weekend(friday, fsf, sfs)
                next_vars.append(clone)
        return next_vars

    def _generate_relaxed_variants(self, variants, friday, fixed, pre_weekend_assignments, 
                                 next_vars):
        """Generate variants with relaxed rotation rules."""
        self._rotation_enforced = False
        for var in variants:
            pairs = self._get_valid_nurse_pairs(
                friday, var.state.last_assignment, var.state.last_pattern,
                fixed, var.state.weekend_tracking,
                schedule=var.state.schedule,
                all_pre_scheduled_weekends=pre_weekend_assignments,
                enforce_rotation=False,
                nurses_allowed_rotation_violation=self.nurses_allowed_rotation_violation,
            )
            for fsf, sfs in pairs:
                # Track violations for reporting
                self._track_rotation_violations(var, friday, fsf, sfs)
                clone = var.clone()
                clone.assign_weekend(friday, fsf, sfs)
                next_vars.append(clone)
        return next_vars

    def _track_rotation_violations(self, var, friday, fsf, sfs):
        """Track rotation violations for reporting purposes."""
        for nurse, new_pat in ((fsf, WeekendPattern.FSF), (sfs, WeekendPattern.SFS)):
            if var.state.last_pattern.get(nurse) == new_pat:
                self.rotation_violation_history[nurse].append(friday)
                self._rotation_violations.append((friday.isoformat(), nurse, new_pat.value))

    # =====================================================================
    # STATE AND UTILITY METHODS
    # =====================================================================

    def get_state_snapshot(self) -> ScheduleState:
        """Get a snapshot of the current scheduling state."""
        weekend_lists = {}
        for nurse in self.nurses:
            historic = [w for w in self.weekend_history.get_weekends(nurse)
                       if w < self.start_date]
            historic.sort()
            weekend_lists[nurse] = historic

        return ScheduleState(
            self.schedule,
            self.main_assignment_counts,
            self.backup_assignment_counts,
            self.last_assignment,
            self.last_pattern,
            self.weekend_tracking,
            nurse_weekend_lists=weekend_lists
        )

    @staticmethod
    def is_empty(value) -> bool:
        """
        Check if a given value is considered empty or unassigned.
        Args:
            value: The value to check (typically a nurse name or assignment).
        Returns:
            True if the value is None, NaN, empty string, or whitespace-only string; False otherwise.
        """
        return is_empty(value)

    def get_nurse_assignment_counts(self, variant: ScheduleVariant) -> dict:
        """Get assignment counts for all nurses in a variant."""
        counts = {}
        for nurse in self.nurses:
            main_count = (variant.state.schedule['main'] == nurse).sum()
            backup_count = (variant.state.schedule['backup'] == nurse).sum()
            counts[nurse] = {
                "main": int(main_count),
                "backup": int(backup_count),
                "total": int(main_count + backup_count)
            }
        return counts

    # =====================================================================
    # CONFIGURATION METHODS
    # =====================================================================

    def set_allow_rotation_violations(self, allow: bool):
        """Set whether rotation violations are allowed."""
        self.allow_rotation_violations = allow

    def set_nurses_allowed_rotation_violation(self, nurses: list[str]):
        """
        Set the set of nurses allowed to violate the rotation rule.
        If nurses is empty and allow_rotation_violations is True, treat as all nurses allowed.
        """
        if getattr(self, "allow_rotation_violations", False) and not nurses:
            self.nurses_allowed_rotation_violation = set(self.nurses)
        else:
            self.nurses_allowed_rotation_violation = set(nurses)

    def get_rotation_violation_history(self) -> dict[str, list[pd.Timestamp]]:
        """Get the history of rotation violations."""
        return self.rotation_violation_history

    # =====================================================================
    # EXPORT AND OUTPUT METHODS
    # =====================================================================

    def export_weekend_variants(self, filename: str):
        """
        Generate the weekend variants (weekend assignments only, before weekday assignment)
        and write them to a text file for manual review.
        """
        weekend_variants = self.generate_all_weekend_variants()
        with open(filename, "w") as f:
            for idx, variant in enumerate(weekend_variants):
                # Extract only the weekend rows from the variant schedule:
                weekend_df = variant.state.schedule[variant.state.schedule['is_weekend']]
                f.write(f"Variant {idx}\n")
                f.write(weekend_df[['main', 'backup']].to_string())
                f.write("\n" + "-"*40 + "\n")

    def _export_variant_pdf(self, pdf_path: str, sched_df: "pd.DataFrame",
                          cal: "calendar.Calendar") -> None:
        """Delegate to :func:`scheduler.exporters.pdf.export_variant_pdf`."""
        _export_variant_pdf_fn(pdf_path, sched_df, cal, self.PDF_FONT_SIZES)

    def _draw_weekday_header(self, cvs, hdr_y_top, row_h, col_w):
        _draw_weekday_header_fn(cvs, hdr_y_top, row_h, col_w, self.PDF_FONT_SIZES)

    def _draw_week_rows(self, cvs, weeks, year, month, sched_df, y_top, row_h, col_w):
        _draw_week_rows_fn(
            cvs, weeks, year, month, sched_df, y_top, row_h, col_w,
            self.PDF_FONT_SIZES,
        )

    # =====================================================================
    # MAIN SCHEDULE GENERATION METHOD
    # =====================================================================

    class WeekendVariantMode(str, Enum):
        """Controls how weekend variants are generated."""
        STRICT_ONLY = "strict_only"
        STRICT_THEN_RELAXED = "strict_then_relaxed"
        RELAXED_ALLOWED = "relaxed_allowed"

    @classmethod
    def _normalize_weekend_variant_mode(
        cls,
        weekend_variant_mode: str | "NurseScheduler.WeekendVariantMode",
    ) -> "NurseScheduler.WeekendVariantMode":
        """Normalize weekend variant mode from enum or string input."""
        if isinstance(weekend_variant_mode, cls.WeekendVariantMode):
            return weekend_variant_mode
        try:
            return cls.WeekendVariantMode(weekend_variant_mode)
        except ValueError as ex:
            valid_modes = ", ".join(mode.value for mode in cls.WeekendVariantMode)
            raise ValueError(
                f"Invalid weekend_variant_mode={weekend_variant_mode!r}. "
                f"Expected one of: {valid_modes}."
            ) from ex

    def generate_schedule(self, top_n: int = 10, max_workers: int = 2, *,
                         confirm_rotation_callback: Optional[Callable[[], bool]] = None,
                         weekend_variant_mode: str | "NurseScheduler.WeekendVariantMode" = WeekendVariantMode.STRICT_THEN_RELAXED,
                         profile_performance: Optional[bool] = None,
                         profile_output_path: Optional[str | os.PathLike[str]] = None) -> list:
        """Generate schedules and optionally capture detailed performance metrics."""
        sleep_handle = inhibit_sleep()

        try:
            profiling_enabled = PERFORMANCE_PROFILING_REQUESTED if profile_performance is None else bool(profile_performance)
            if profiling_enabled and psutil is None:
                logger.warning(
                    "Performance profiling requested but psutil is not available. "
                    "Install psutil or disable profiling to silence this message."
                )
                profiling_enabled = False

            profile_json_path: Optional[str | os.PathLike[str]]
            if profile_output_path is None:
                profile_json_path = PERFORMANCE_PROFILE_JSON_DEFAULT
            else:
                profile_json_path = profile_output_path

            # Setup rotation confirmation callback
            confirm_rotation_callback = self._setup_rotation_callback(confirm_rotation_callback)

            # Generate weekend variants
            variants = self._generate_weekend_variants(confirm_rotation_callback, weekend_variant_mode)
            if not variants:
                return []

            # Evaluate variants with or without profiling
            if profiling_enabled:
                candidate_schedules, worker_metrics = self._evaluate_variants_with_profiling(variants, max_workers)
            else:
                candidate_schedules = self._evaluate_variants(variants, max_workers)
                worker_metrics = []

            if not candidate_schedules:
                logger.error("❌ No candidate schedules after evaluation.")
                return []

            # Score and rank variants
            self._score_and_rank_variants(candidate_schedules)

            # Export best variants as PDFs
            self._export_top_variants_as_pdfs(candidate_schedules, top_n)

            # Optional timing summary
            self._print_timing_summary(candidate_schedules)

            if profiling_enabled:
                self._report_performance_metrics(worker_metrics, profile_json_path)

            return candidate_schedules[:top_n]

        finally:
            allow_sleep(sleep_handle)

    def _setup_rotation_callback(self, confirm_rotation_callback):
        """Setup the callback for rotation confirmation."""
        if confirm_rotation_callback is None:
            def _default_confirm() -> bool:
                try:
                    return InputValidator.confirm_action(
                        "\nNo valid schedules were found with the strict "
                        "weekend-rotation rule.\n"
                        "Allow rotation repeats and try again?"
                    )
                except Exception:
                    return False
            confirm_rotation_callback = _default_confirm
        return confirm_rotation_callback

    def _generate_weekend_variants(self, confirm_rotation_callback, weekend_variant_mode):
        """Generate weekend variants based on the selected fallback mode."""
        mode = self._normalize_weekend_variant_mode(weekend_variant_mode)

        if mode == self.WeekendVariantMode.STRICT_ONLY:
            variants = self.generate_all_weekend_variants(allow_rotation_violations=False)
            if not variants:
                logger.info("No feasible variants with strict rotation mode.")
            return variants

        if mode == self.WeekendVariantMode.RELAXED_ALLOWED:
            variants = self.generate_all_weekend_variants(allow_rotation_violations=True)
            if not variants:
                logger.error("No feasible variants with relaxed rotation mode.")
            return variants

        # STRICT_THEN_RELAXED
        variants = self.generate_all_weekend_variants(allow_rotation_violations=False)
        if not variants:
            if not confirm_rotation_callback():
                logger.info("User declined to allow rotation repeats – abort.")
                return []
            variants = self.generate_all_weekend_variants(allow_rotation_violations=True)
            if not variants:
                logger.error("Still no feasible variants with repeats allowed.")
                return []
        return variants

    def _evaluate_variants(self, variants, max_workers):
        """Evaluate all variants either in parallel or serially."""
        candidate_schedules: list = []
        workers = min(max_workers, os.cpu_count() or 1, len(variants))

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                fut_map = {
                    pool.submit(_evaluate_variant_worker, (i, v)): i
                    for i, v in enumerate(variants)
                }
                for fut in tqdm(as_completed(fut_map), total=len(fut_map),
                              desc="Evaluating variants", unit="variant"):
                    try:
                        candidate_schedules.append(fut.result())
                    except Exception as ex:
                        logger.error(f"Worker {fut_map[fut]} failed: {ex}")
        except Exception as e:
            # Fallback: run serially
            logger.warning(f"ProcessPool failed ({e}); evaluating serially.")
            for idx, var in enumerate(variants):
                candidate_schedules.append(_evaluate_variant_worker((idx, var)))

        return candidate_schedules

    def _evaluate_variants_with_profiling(self, variants, max_workers):
        """Evaluate all variants while collecting profiling metrics."""
        candidate_schedules: list = []
        all_worker_metrics: list[WorkerMetrics] = []
        workers = min(max_workers, os.cpu_count() or 1, len(variants))

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                fut_map = {
                    pool.submit(_evaluate_variant_worker_profiled, (i, v)): i
                    for i, v in enumerate(variants)
                }
                for fut in tqdm(as_completed(fut_map), total=len(fut_map),
                                desc="Evaluating variants", unit="variant"):
                    try:
                        idx, stats, nurse_counts, sched_df, metrics = fut.result()
                        candidate_schedules.append((idx, stats, nurse_counts, sched_df))
                        all_worker_metrics.append(metrics)
                    except Exception as ex:
                        logger.error(f"Worker {fut_map[fut]} failed: {ex}")
        except Exception as e:
            logger.warning(f"ProcessPool failed ({e}); evaluating serially with profiling.")
            for idx, var in enumerate(variants):
                try:
                    result = _evaluate_variant_worker_profiled((idx, var))
                    idx, stats, nurse_counts, sched_df, metrics = result
                    candidate_schedules.append((idx, stats, nurse_counts, sched_df))
                    all_worker_metrics.append(metrics)
                except Exception as ex:
                    logger.error(f"Serial worker {idx} failed: {ex}")

        return candidate_schedules, all_worker_metrics

    def _report_performance_metrics(self, worker_metrics: list[WorkerMetrics],
                                    output_path: Optional[str | os.PathLike[str]]) -> None:
        """Print and optionally export performance metrics."""
        if not worker_metrics:
            logger.info("Performance profiling collected no metrics to report.")
            return

        report = PerformanceReport(worker_metrics)
        report.print_summary()

        if output_path:
            try:
                report.export_json(os.fspath(output_path))
            except Exception as exc:
                logger.error(f"Failed to export performance metrics: {exc}")

    def _score_and_rank_variants(self, candidate_schedules):
        """Compute weighted scores and rank variants.

        Semantics alignment note:
        - ``BestStateTracker`` uses lexicographic ``ScheduleQuality`` comparison
          during intra-variant local search.
        - This method performs *inter-variant* final ranking by normalizing and
          weighting metrics. ``long_term`` uses the same ``_long_term_score``
          objective as ``ScheduleQuality.history_penalty`` (lower is better).
        """
        viol_counts = self.weekend_history.get_violation_counts()
        overage = self._historic_overage()
        weights = self.config.scoring_weights

        rows = []
        for idx, stats, nurse_counts, sched_df in candidate_schedules:
            rows.append({
                "idx": idx,
                "rotation_rep": stats["rotation_rep"],
                "gaps": stats["gaps"],
                "rot_viol": self._rotation_violation_score(nurse_counts, viol_counts, sched_df),
                "weekend_gap": self._weekend_gap_penalty(sched_df),
                "balance": stats["balance_main"] + stats["balance_backup"],
                "long_term": self._long_term_score(nurse_counts, overage),
            })

        metric_df = weighted_scores_from_rows(rows, weights=weights)

        # Attach score back to stats dict
        for idx, stats, _, _ in candidate_schedules:
            stats["weighted_score"] = float(metric_df.loc[idx, "weighted_score"])

        # Rank by weighted score (lower = better)
        candidate_schedules.sort(key=lambda tpl: tpl[1]["weighted_score"])

    def _export_top_variants_as_pdfs(self, candidate_schedules, top_n):
        """Export the best variants as PDF files."""
        cal = calendar.Calendar(firstweekday=6)  # Sunday-first
        pdf_paths = []
        
        for rank, (idx, stats, nurse_counts, sched_df) in enumerate(
                candidate_schedules[:top_n], start=1):
            pdf_name = f"schedule_variant_{rank}.pdf"
            self._export_variant_pdf(pdf_name, sched_df, cal)
            pdf_paths.append(pdf_name)

        print(f"[scheduler] wrote {len(pdf_paths)} PDF file(s): {', '.join(pdf_paths)}")

    def _print_timing_summary(self, candidate_schedules):
        """Print timing summary if enabled."""
        if MEASURE_PHASE_TIMES and candidate_schedules:
            n = len(candidate_schedules)
            phases = ["t_clone", "t_assign", "t_gapfill", "t_rebalance", "t_total"]
            labels = ["clone", "assign weekdays", "gap-fill", "rebalance", "TOTAL"]
            sums = [sum(cs[1].get(p, 0) for cs in candidate_schedules) for p in phases]
            
            print("\n=== Average phase times per variant (seconds) ===")
            for lbl, total in zip(labels, sums):
                print(f" {lbl:17}: {total / n:.4f}")
            print("=================================================\n")


