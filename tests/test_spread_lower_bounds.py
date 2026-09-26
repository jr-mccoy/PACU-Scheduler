"""Step 2 of docs/scheduler-optimization-audit.md (finding 7).

The refill passes used to stop once the backup and main spreads were both at
most 1, although total spread, or a spread of 0, could still improve. They
now stop only at proven lower bounds, which these tests check against every
legal fill of small instances.
"""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import weekday_only_variant

import scheduler.evaluation.worker as worker
from scheduler import WorkerTuningConfig
from scheduler.domain import _spread_lower_bound
from scheduler.evaluation.worker import _evaluate_variant_core

# With weekday rows only and 2-day spacing, a Tuesday or Wednesday nurse can
# work no other day that week, so a week needs six nurses to fill.
NURSES = ["N1", "N2", "N3", "N4", "N5", "N6"]
MONDAY = pd.Timestamp("2026-01-05")


def _day(offset: int) -> pd.Timestamp:
    return MONDAY + pd.Timedelta(days=offset)


def _complete_fills(variant):
    """Yield once per complete fill of the weekday slots, in gap-fill rules.

    Gap-fill rules are the most permissive the searches use, so every
    schedule any of them can produce is among these.
    """
    slots = [
        (day, role)
        for day in variant.get_weekdays()
        for role in ("main", "backup")
        if not variant.is_pre_scheduled(day, role) and not variant.is_unfillable(day, role)
    ]

    def fill(k):
        if k == len(slots):
            yield
            return
        day, role = slots[k]
        for nurse in variant.get_eligible_nurses_for_day_gap(day, role):
            variant.inc_assign(day, role, nurse, gap_phase=True)
            yield from fill(k + 1)
            variant.dec_assign(day, role, nurse)

    yield from fill(0)


def _check_bounds_against_every_fill(variant):
    """Bounds are the same in every complete fill and never above its spreads."""
    variant.compute_unfillable_slots()
    lowest = None
    bounds_seen = set()
    fills = 0
    for _ in _complete_fills(variant):
        fills += 1
        spreads = variant.spread_components()
        bounds_seen.add(variant.spread_lower_bounds())
        lowest = spreads if lowest is None else tuple(map(min, lowest, spreads))
    assert fills > 0
    assert len(bounds_seen) == 1
    (bounds,) = bounds_seen
    assert all(b <= low for b, low in zip(bounds, lowest, strict=True)), (bounds, lowest)
    return bounds, lowest


# ── the bound itself ───────────────────────────────────────────────────────
def test_a_spread_of_zero_needs_the_shifts_to_divide_evenly():
    even = pd.Series([2, 2, 2, 2], index=list("ABCD"))
    odd = pd.Series([3, 2, 2, 2], index=list("ABCD"))
    loose = dict.fromkeys("ABCD", 0), dict.fromkeys("ABCD", 10)
    assert _spread_lower_bound(even, *loose) == 0
    assert _spread_lower_bound(odd, *loose) == 1


def test_fixed_shifts_and_capacity_raise_the_bound():
    counts = pd.Series([2, 2, 2, 2], index=list("ABCD"))
    caps = dict.fromkeys("ABCD", 10)
    # A already has 4 fixed shifts; someone must have at most floor(8/4) = 2.
    assert _spread_lower_bound(counts, {"A": 4}, caps) == 2
    # D can never have more than 0; someone must have at least 2.
    assert _spread_lower_bound(counts, {}, {**caps, "D": 0}) == 2
    # Both at once: at least 4 against at most 0.
    assert _spread_lower_bound(counts, {"A": 4}, {**caps, "D": 0}) == 4


# ── against every legal fill ───────────────────────────────────────────────
def test_bounds_hold_for_every_fill_of_an_open_week():
    variant = weekday_only_variant(NURSES, weeks=1)
    bounds, lowest = _check_bounds_against_every_fill(variant)
    assert bounds == lowest == (1, 1, 1)  # 4 + 4 shifts over 6 nurses


def test_bounds_hold_with_time_off_and_a_pinned_cell():
    variant = weekday_only_variant(
        [*NURSES, "N7"], weeks=1, pre_scheduled={_day(3): {"main": "N1", "backup": None}}
    )
    variant.availability["N7"] = False  # N7 is off all week

    bounds, lowest = _check_bounds_against_every_fill(variant)
    # 8 shifts over 7 nurses divide unevenly, which alone bounds the total
    # spread at 1. N7 can have none, so somebody has 2: the bound is 2.
    assert bounds == lowest == (1, 1, 2)


def test_bounds_hold_around_an_unfillable_day():
    variant = weekday_only_variant(NURSES[:5], weeks=1)
    variant.availability.loc[_day(2)] = False  # nobody can work Wednesday

    bounds, lowest = _check_bounds_against_every_fill(variant)
    assert variant.unfillable_slots == {(_day(2), "main"), (_day(2), "backup")}
    assert bounds == lowest == (1, 1, 1)  # 3 + 3 shifts over 5 nurses


def test_there_is_no_bound_while_a_fillable_slot_is_empty():
    variant = weekday_only_variant(NURSES, weeks=1)
    variant.compute_unfillable_slots()
    assert variant.spread_lower_bounds() is None
    assert variant.spreads_at_lower_bound() is False

    next(_complete_fills(variant))
    assert variant.spread_lower_bounds() is not None


# ── where the passes stop ──────────────────────────────────────────────────
@pytest.mark.parametrize("at_bound", [True, False])
def test_window_refill_stops_only_at_the_bound(monkeypatch, at_bound):
    variant = weekday_only_variant(NURSES, weeks=4)
    next(_complete_fills(variant))
    searched = []

    def backtrack(vars_list, *args, **kwargs):
        searched.append(vars_list)
        return False

    monkeypatch.setattr(variant, "spreads_at_lower_bound", lambda: at_bound)
    monkeypatch.setattr(variant.window_optimizer, "backtrack_window", backtrack)
    variant.iterative_window_refill_rebalance(max_passes=1)

    assert bool(searched) is not at_bound


@pytest.mark.parametrize("at_bound", [True, False])
def test_the_worker_runs_the_full_period_refill_until_the_bound(monkeypatch, at_bound):
    calls = []

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
                {"main": ["A", "B"], "backup": ["B", "A"], "is_weekend": False}, index=idx
            )
            self.state.main_assignment_counts = pd.Series([1, 1], index=self.nurses)
            self.state.backup_assignment_counts = pd.Series([1, 1], index=self.nurses)
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
            calls.append(("window", kwargs["target_spread"]))

        def iterative_full_period_refill(self, **kwargs):
            calls.append(("full", kwargs["target_spread"]))

        def spread_components(self):
            return (1, 1, 2)  # within the old (1, 1) target

        def spreads_at_lower_bound(self):
            return at_bound

    monkeypatch.setattr(worker, "BestStateTracker", _Tracker)
    _evaluate_variant_core((0, _Variant(), WorkerTuningConfig()), with_profiling=False)

    expected = [("window", None)] + ([] if at_bound else [("full", None)])
    assert calls == expected


def test_a_fixed_target_still_stops_as_before():
    variant = weekday_only_variant(NURSES, weeks=4)
    next(_complete_fills(variant))
    spread_b, spread_m, _ = variant.spread_components()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            variant.window_optimizer,
            "backtrack_window",
            lambda *a, **k: pytest.fail("a met fixed target should not search"),
        )
        variant.iterative_window_refill_rebalance(max_passes=1, target_spread=(spread_b, spread_m))
