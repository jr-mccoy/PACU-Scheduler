"""Step 1 of docs/scheduler-optimization-audit.md.

Two changes make the weekday search faster without changing what it
produces, and one stops it discarding improvements:

* the per-weekday tie-breaker counts are counted from the rows directly
  instead of through pandas ``value_counts`` (finding 1);
* ``_assign_slot_sequence`` places the first nurse of a freshly built domain
  without re-running the eligibility check that built it (finding 2);
* the worker keeps a full-period refill that improves the schedule even when
  it misses the target spread (finding 8).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

import scheduler.evaluation.worker as worker
from scheduler import SchedulerConfig, ScheduleState, ScheduleVariant, WorkerTuningConfig
from scheduler.evaluation.worker import _evaluate_variant_core

QUICK = WorkerTuningConfig(
    gap_fill_iterations=3,
    rebalance_iterations=3,
    window_refill_max_passes=2,
    window_refill_time_limit_ms=500,
    full_period_max_orders=2,
    full_period_per_attempt_time_ms=500,
)

# Time off that leaves some weekday domains small, so the searches backtrack.
TIME_OFF = [
    ("A", "2026-11-03"),
    ("B", "2026-11-03"),
    ("C", "2026-11-04"),
    ("D", "2026-11-10"),
    ("E", "2026-11-11"),
    ("F", "2026-11-11"),
]


def _value_counts_truth(variant: ScheduleVariant, weekday: int) -> dict:
    """What ``_weekday_counts_for`` computed before: pandas value_counts."""
    sched = variant.state.schedule
    sub = sched.loc[~sched["is_weekend"], ["main", "backup"]]
    sub = sub[sub.index.weekday == weekday]
    if sub.empty:
        return {}
    counts = sub["main"].value_counts().add(sub["backup"].value_counts(), fill_value=0)
    return counts.astype(int).to_dict()


def _variant(tmp_path, **config):
    db = seed_db(tmp_path, time_off=TIME_OFF)
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15", **config)
    return scheduler.generate_all_weekend_variants()[0]


# ── finding 1: weekday tie-breaker counts ──────────────────────────────────
def test_weekday_counts_match_value_counts_throughout_an_evaluation(tmp_path, monkeypatch):
    calls = {"n": 0}
    mismatches = []
    original = ScheduleVariant._weekday_counts_for

    def checked(self, weekday):
        result = original(self, weekday)
        calls["n"] += 1
        if weekday in (0, 1, 2, 3) and result != _value_counts_truth(self, weekday):
            mismatches.append(weekday)
        return result

    monkeypatch.setattr(ScheduleVariant, "_weekday_counts_for", checked)
    _evaluate_variant_core((0, _variant(tmp_path), QUICK), with_profiling=False)

    assert calls["n"] > 100, "the evaluation barely used the weekday counts"
    assert mismatches == []


def test_weekday_counts_skip_missing_cells_and_weekends_like_value_counts(tmp_path):
    variant = _variant(tmp_path)
    variant.assign_weekdays()
    sched = variant.state.schedule
    monday, tuesday = pd.Timestamp("2026-11-02"), pd.Timestamp("2026-11-09")
    sched.at[monday, "main"] = np.nan
    sched.at[monday, "backup"] = None
    sched.at[tuesday - pd.Timedelta(days=1), "main"] = "Z"  # a Sunday: not counted
    variant._invalidate_weekday_cache()

    for weekday in (0, 1, 2, 3):
        assert variant._weekday_counts_for(weekday) == _value_counts_truth(variant, weekday)
    assert variant._weekday_counts_for(4) == {}


# ── finding 2: no second eligibility check ─────────────────────────────────
@pytest.mark.parametrize(
    "config",
    [
        {},
        {"allow_one_day_weekday_gap": True},
        {"allow_midweek_pair_mixed": True, "allow_midweek_pair_backup_only": True},
    ],
    ids=["default", "one-day-gap", "midweek-pairs"],
)
def test_slot_sequences_only_place_nurses_the_full_check_accepts(tmp_path, monkeypatch, config):
    """Every nurse placed unchecked is one ``_inc_assign`` would have placed."""
    active: list[bool] = []  # gap_mode of the _assign_slot_sequence running now
    placed = {"n": 0}
    rejected = []
    original_sequence = ScheduleVariant._assign_slot_sequence
    original_place = ScheduleVariant._place_assignment

    def sequence(self, slots, *, gap_mode=False, **kwargs):
        active.append(gap_mode)
        try:
            return original_sequence(self, slots, gap_mode=gap_mode, **kwargs)
        finally:
            active.pop()

    def place(self, date, role, nurse):
        if active:
            placed["n"] += 1
            if not self._can_inc_assign(date, role, nurse, gap_phase=active[-1]):
                rejected.append((date, role, nurse))
        return original_place(self, date, role, nurse)

    monkeypatch.setattr(ScheduleVariant, "_assign_slot_sequence", sequence)
    monkeypatch.setattr(ScheduleVariant, "_place_assignment", place)
    _evaluate_variant_core((0, _variant(tmp_path, **config), QUICK), with_profiling=False)

    assert placed["n"] > 50, "the evaluation barely ran slot sequences"
    assert rejected == []


# ── finding 8: full-period refill keeps improvements ───────────────────────
def _weekday_only_variant(nurses: list[str], weeks: int = 4) -> ScheduleVariant:
    days = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-05") + pd.Timedelta(weeks=w, days=d)
            for w in range(weeks)
            for d in range(4)
        ]
    )
    schedule = pd.DataFrame(index=days, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = False
    counts = pd.Series(0, index=pd.Index(nurses))

    class _Nurses:
        def is_prn_nurse(self, nurse):
            return False

        def is_late_shift_nurse(self, nurse):
            return False

    state = ScheduleState(schedule, counts, counts.copy(), {}, {})
    return ScheduleVariant(
        state,
        nurses,
        pd.DataFrame(True, index=days, columns=nurses),
        SchedulerConfig(),
        _Nurses(),
        console_debug=False,
    )


def test_full_period_refill_keeps_an_improvement_that_misses_the_target():
    nurses = [f"N{i}" for i in range(1, 9)]
    variant = _weekday_only_variant(nurses)
    # N8 is never available, so no schedule reaches spreads of (1, 1).
    variant.availability["N8"] = False
    # Start from a lopsided schedule: N1 has every Main, N2 every Backup.
    variant.state.schedule["main"] = "N1"
    variant.state.schedule["backup"] = "N2"
    variant._recalculate_assignment_counts()
    before = variant._spread_components()

    variant.iterative_full_period_refill(
        max_orders=2,
        per_attempt_time_ms=2_000,
        per_attempt_nodes=50_000,
        target_spread=(1, 1),
        required_spread=False,
    )

    after = variant._spread_components()
    assert after < before
    assert after[0] > 1 or after[1] > 1  # the target was missed, and still kept


def test_the_worker_keeps_full_period_improvements_that_miss_the_target(monkeypatch):
    received = {}

    class _Tracker:
        def __init__(self, variant):
            pass

        def initialize(self):
            return None

        def restore_global_best(self):
            return None

        def get_global_best_quality(self):
            return None

        def get_statistics(self):
            return {}

    class _Variant:
        nurses = ["A", "B"]
        unfillable_slots = frozenset()

        def __init__(self):
            idx = pd.date_range("2026-01-05", periods=2, freq="D")
            self.state = type("State", (), {})()
            self.state.schedule = pd.DataFrame(
                {"main": ["A", "A"], "backup": ["B", "B"], "is_weekend": False}, index=idx
            )
            self.state.main_assignment_counts = pd.Series([2, 0], index=self.nurses)
            self.state.backup_assignment_counts = pd.Series([0, 2], index=self.nurses)
            self.state.rotation_repeats = 0

        def clone(self):
            return self

        def compute_unfillable_slots(self):
            return self.unfillable_slots

        def assign_weekdays(self):
            pass

        def iterative_gap_fill_no_revert(self, **kwargs):
            pass

        def iterative_rebalance_no_revert(self, **kwargs):
            pass

        def iterative_window_refill_rebalance(self, **kwargs):
            pass

        def iterative_full_period_refill(self, **kwargs):
            received.update(kwargs)

        def _spread_components(self):
            return (2, 2, 0)  # above the target, so the full-period refill runs

    monkeypatch.setattr(worker, "BestStateTracker", _Tracker)
    _evaluate_variant_core((0, _Variant(), QUICK), with_profiling=False)

    assert received.get("required_spread") is False
