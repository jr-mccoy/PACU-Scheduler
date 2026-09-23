"""Parity tests for the consolidated variant-evaluation worker.

After Phase 2b the two public workers share a single implementation
(``_evaluate_variant_core``). These tests assert that consolidation
without running the heavy multi-variant optimization hot path — we stub
out the variant and verify both paths drive the same algorithm.
"""

from __future__ import annotations

import pandas as pd
import pytest

import scheduler.legacy_core as legacy_core
from scheduler import (
    WorkerMetrics,
    _evaluate_variant_worker,
    _evaluate_variant_worker_profiled,
)
from scheduler.engine import WorkerTuningConfig


def test_workers_share_a_single_core_implementation():
    """Both public workers must funnel into ``_evaluate_variant_core``."""
    core_name = "_evaluate_variant_core"
    assert core_name in _evaluate_variant_worker.__code__.co_names
    assert core_name in _evaluate_variant_worker_profiled.__code__.co_names


def test_engine_module_reexports_consolidated_worker():
    """``scheduler.engine`` remains the documented public entrypoint."""
    import scheduler.engine as engine

    assert engine._evaluate_variant_worker is _evaluate_variant_worker
    assert engine._evaluate_variant_worker_profiled is _evaluate_variant_worker_profiled


class _StubVariant:
    """Minimal variant that satisfies the worker contract without optimization."""

    def __init__(self, calls: list[str] | None = None):
        self.calls: list[str] = calls if calls is not None else []
        self.nurses = ["Alice", "Bob"]
        idx = pd.date_range("2026-01-05", periods=3, freq="D")
        schedule = pd.DataFrame(
            index=idx,
            data={"main": [None, None, None], "backup": [None, None, None]},
        )
        schedule["is_weekend"] = False
        counts = pd.Series([0, 0], index=pd.Index(self.nurses))

        class _State:
            pass

        self.state = _State()
        self.state.schedule = schedule
        self.state.main_assignment_counts = counts
        self.state.backup_assignment_counts = counts.copy()
        self.state.rotation_repeats = 0
        self.unfillable_slots = frozenset()

    # ------------------------------------------------------------------ stubs
    def clone(self):
        self.calls.append("clone")
        return _StubVariant(self.calls)

    def compute_unfillable_slots(self):
        return self.unfillable_slots

    def assign_weekdays(self):
        self.calls.append("assign_weekdays")

    def iterative_gap_fill_no_revert(self, **_kwargs):
        self.calls.append("gap_fill")

    def iterative_rebalance_no_revert(self, **_kwargs):
        self.calls.append("rebalance")

    def iterative_window_refill_rebalance(self, **_kwargs):
        self.calls.append("window_refill")

    def iterative_full_period_refill(self, **_kwargs):  # pragma: no cover - not triggered
        self.calls.append("full_period_refill")

    def _spread_components(self):
        # Returns (s_b, s_m, _) with both spreads at the target so full-period
        # refill is short-circuited.
        return (1, 1, 0)


class _StubTracker:
    """Stand-in for ``BestStateTracker`` that records phase calls."""

    def __init__(self, variant):
        self.variant = variant

    def initialize(self):
        return None

    def restore_global_best(self):
        return None

    def get_global_best_quality(self):
        return None

    def get_statistics(self):
        return {"tracker_stat": 42}


@pytest.fixture
def stubbed_worker_env(monkeypatch):
    """Patch the heavy collaborators so both workers run end-to-end in <1s."""
    monkeypatch.setattr(legacy_core, "BestStateTracker", _StubTracker)
    monkeypatch.setattr(
        legacy_core,
        "WORKER_TUNING",
        WorkerTuningConfig(),
    )
    return None


def _strip_timings(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if not k.startswith("t_")}


def test_profiled_and_unprofiled_workers_produce_identical_scoring(stubbed_worker_env):
    variant_a = _StubVariant()
    variant_b = _StubVariant()

    idx_n, stats_n, counts_n, sched_n = _evaluate_variant_worker((7, variant_a))
    idx_p, stats_p, counts_p, sched_p, metrics = _evaluate_variant_worker_profiled((7, variant_b))

    assert idx_n == idx_p == 7
    assert isinstance(metrics, WorkerMetrics)
    assert _strip_timings(stats_n) == _strip_timings(stats_p)
    assert counts_n == counts_p
    pd.testing.assert_frame_equal(sched_n, sched_p)


def test_profiled_worker_emits_metrics_per_phase(stubbed_worker_env):
    variant = _StubVariant()
    _idx, _stats, _counts, _sched, metrics = _evaluate_variant_worker_profiled((1, variant))

    assert metrics.worker_id == 1
    assert metrics.variant_idx == 1
    for required in ("clone", "assign_weekdays", "gap_fill", "rebalance", "window_refill"):
        assert required in metrics.phases, f"missing phase {required!r}"
        assert metrics.phases[required].duration_sec >= 0.0


def test_unprofiled_worker_returns_four_tuple(stubbed_worker_env):
    variant = _StubVariant()
    result = _evaluate_variant_worker((2, variant))
    assert len(result) == 4


def test_both_workers_drive_the_same_algorithm_phases(stubbed_worker_env):
    """Critical parity property: same phases, same order, same algorithm."""
    variant_n = _StubVariant()
    _evaluate_variant_worker((0, variant_n))
    profiled_variant = _StubVariant()
    _evaluate_variant_worker_profiled((0, profiled_variant))

    expected = ["clone", "assign_weekdays", "gap_fill", "rebalance", "window_refill"]
    assert variant_n.calls == expected
    assert profiled_variant.calls == expected


def test_unprofiled_worker_records_timings_when_measure_phase_times(
    stubbed_worker_env, monkeypatch
):
    monkeypatch.setattr(legacy_core, "MEASURE_PHASE_TIMES", True)
    _idx, stats, _counts, _sched = _evaluate_variant_worker((0, _StubVariant()))
    for key in ("t_clone", "t_assign", "t_gapfill", "t_rebalance", "t_lns_2w", "t_total"):
        assert key in stats, f"missing {key} in non-profiled stats: {stats}"


def test_profiled_worker_records_timings_when_measure_phase_times(stubbed_worker_env, monkeypatch):
    monkeypatch.setattr(legacy_core, "MEASURE_PHASE_TIMES", True)
    _idx, stats, _counts, _sched, _metrics = _evaluate_variant_worker_profiled((0, _StubVariant()))
    for key in ("t_clone", "t_assign", "t_gapfill", "t_rebalance", "t_lns_2w", "t_total"):
        assert key in stats, f"missing {key} in profiled stats: {stats}"
