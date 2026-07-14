"""Variant evaluation pipeline used by serial and parallel schedulers."""

from __future__ import annotations

import time
from contextlib import contextmanager

from ..domain import BestStateTracker
from ..profiling import MetricsCollector
from ..runtime import _count_main_backup_empties, _count_weekday_gaps
from .config import WORKER_TUNING

MEASURE_PHASE_TIMES = True


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

__all__ = [
    "_evaluate_variant_core",
    "_evaluate_variant_worker",
    "_evaluate_variant_worker_profiled",
]
