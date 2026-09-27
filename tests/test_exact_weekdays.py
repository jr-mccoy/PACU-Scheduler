"""The exact weekday solve (docs/scheduler-optimization-audit.md, finding 12).

It lists every legal fill of each week with the scheduler's own rules, lets
CP-SAT choose one fill per week, and re-checks what it places. These tests
compare it with brute force over every legal schedule of small instances,
check that it only ever places legal nurses, and check that it steps aside
for the search when it cannot promise an exact answer.
"""

from __future__ import annotations

import builtins
import dataclasses

import pandas as pd
import pytest
from scheduling_fixtures import REGULAR, build_scheduler, seed_db, weekday_only_variant

from scheduler import WorkerTuningConfig
from scheduler.domain import ScheduleQuality
from scheduler.evaluation.worker import _evaluate_variant_core
from scheduler.optimization.exact_weekdays import solve_weekdays_exactly, unavailable_reason

NURSES = ["N1", "N2", "N3", "N4", "N5", "N6"]
MONDAY = pd.Timestamp("2026-01-05")


def _day(offset: int) -> pd.Timestamp:
    return MONDAY + pd.Timedelta(days=offset)


def _spreads(variant) -> tuple[int, int, int, int]:
    """(unfilled weekday slots, backup, main, total spread)."""
    backup, main, total = variant.spread_components()
    return variant._count_fillable_weekday_gaps(), backup, main, total


def balanced(key):
    gaps, backup, main, total = key
    return (gaps, backup + main, max(backup, main), total)


def backup_first(key):
    return key


def _best_by_brute_force(variant, order) -> tuple:
    """The best key over every legal fill of all weekday slots.

    Fills with no empty slot first, then with at most one, and so on: fewer
    gaps always rank first, so the first round with any fill has the best.
    """
    slots = [
        (day, role)
        for day in variant.get_weekdays()
        for role in ("main", "backup")
        if not variant.is_pre_scheduled(day, role) and not variant.is_unfillable(day, role)
    ]
    for max_empty in range(len(slots) + 1):
        best = None

        def fill(k, empties, max_empty=max_empty):
            nonlocal best
            if k == len(slots):
                key = order(_spreads(variant))
                best = key if best is None or key < best else best
                return
            day, role = slots[k]
            for nurse in variant.get_eligible_nurses_for_day(day, role):
                variant.place_assignment(day, role, nurse)
                fill(k + 1, empties, max_empty)
                variant.dec_assign(day, role, nurse)
            if empties < max_empty:
                fill(k + 1, empties + 1, max_empty)  # or leave the slot empty

        fill(0, 0)
        if best is not None:
            return best
    return None


def _small_two_week_variant(*, short_week: bool = False):
    """Week 1 is open; week 2 is fully pinned, with uneven counts.

    Week 2 only fixes each nurse's starting counts (it is too far from week
    1 to affect its rules), so brute force covers week 1's 720 fills. On
    this instance the two objectives have different optima. With
    ``short_week`` one nurse is off all of week 1, which leaves too few
    nurses to fill it under the spacing rule.
    """
    pinned = {
        _day(7): {"main": "N6", "backup": "N4"},
        _day(8): {"main": "N6", "backup": "N4"},
        _day(9): {"main": "N4", "backup": "N5"},
        _day(10): {"main": "N4", "backup": "N2"},
    }
    variant = weekday_only_variant(NURSES, weeks=2, pre_scheduled=pinned)
    if short_week:
        for offset in range(4):
            variant.availability.loc[_day(offset), "N6"] = False
    variant.compute_unfillable_slots()
    return variant


# ── optimality ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("short_week", [False, True], ids=["fillable", "gaps"])
@pytest.mark.parametrize(
    ("objective", "order"),
    [("balanced", balanced), ("backup_first", backup_first)],
)
def test_the_exact_solve_matches_brute_force(objective, order, short_week):
    variant = _small_two_week_variant(short_week=short_week)
    expected = _best_by_brute_force(variant.clone(), order)

    outcome = solve_weekdays_exactly(variant, objective=objective)

    assert outcome is not None and outcome.optimal
    assert order(_spreads(variant)) == expected
    assert outcome.gaps == expected[0]
    assert (expected[0] > 0) is short_week


def test_the_two_objectives_really_differ_here():
    variant = _small_two_week_variant()
    assert _best_by_brute_force(variant.clone(), balanced) == (0, 3, 2, 3)
    assert _best_by_brute_force(variant.clone(), backup_first) == (0, 1, 3, 3)


def _placed_nurses(variant):
    grid = variant.state.grid()
    for day in variant.get_weekdays():
        for role in ("main", "backup"):
            nurse = grid.get(day, role)
            if not variant.is_pre_scheduled(day, role) and nurse is not None:
                yield day, role, nurse


def _fixture_variant(tmp_path):
    db = seed_db(tmp_path, time_off=[("A", "2026-11-03"), ("B", "2026-11-11")])
    variant = build_scheduler(db, "2026-11-02", "2026-11-29").generate_all_weekend_variants()[0]
    variant = variant.clone()
    variant.compute_unfillable_slots()
    return variant


def test_every_nurse_it_places_is_legal(tmp_path):
    variant = _fixture_variant(tmp_path)
    assert solve_weekdays_exactly(variant) is not None

    for day, role, nurse in _placed_nurses(variant):
        variant.dec_assign(day, role, nurse)
        legal = nurse in variant.get_eligible_nurses_for_day_gap(day, role)
        ordinary = nurse in variant.get_eligible_nurses_for_day(day, role)
        variant.place_assignment(day, role, nurse)
        assert legal, (day, role, nurse)
        # Anything the ordinary rules refuse is the gap-rule exception.
        assert ordinary or variant.is_gap_rule_exception(nurse, day, role)


def test_when_needed_keeps_the_ordinary_rules_in_fillable_weeks(tmp_path):
    variant = _fixture_variant(tmp_path)
    outcome = solve_weekdays_exactly(variant, gap_rule_exceptions="when_needed")

    assert outcome is not None and outcome.relaxed_weeks == []
    assert not any(
        variant.is_gap_rule_exception(nurse, day, role)
        for day, role, nurse in _placed_nurses(variant)
    )


def test_allowing_the_exception_for_balance_is_never_worse(tmp_path):
    strict, loose = _fixture_variant(tmp_path), _fixture_variant(tmp_path)
    solve_weekdays_exactly(strict, gap_rule_exceptions="when_needed")
    solve_weekdays_exactly(loose, gap_rule_exceptions="for_balance")
    assert balanced(_spreads(loose)) <= balanced(_spreads(strict))


def test_it_is_deterministic():
    first, second = _small_two_week_variant(), _small_two_week_variant()
    solve_weekdays_exactly(first)
    solve_weekdays_exactly(second)
    pd.testing.assert_frame_equal(first.state.schedule, second.state.schedule)


def test_unfillable_slots_stay_empty_and_everything_else_is_filled(tmp_path):
    wednesday = pd.Timestamp("2026-11-04")
    db = seed_db(tmp_path, time_off=[(n, wednesday) for n in REGULAR])
    variant = build_scheduler(db, "2026-11-02", "2026-11-15").generate_all_weekend_variants()[0]
    variant = variant.clone()
    variant.compute_unfillable_slots()

    outcome = solve_weekdays_exactly(variant)

    assert outcome is not None and outcome.gaps == 0
    assert variant.state.schedule.loc[wednesday, ["main", "backup"]].isna().all()
    assert variant._count_fillable_weekday_gaps() == 0


# ── stepping aside for the search ──────────────────────────────────────────
def test_spacing_that_reaches_across_weekends_uses_the_search(tmp_path):
    db = seed_db(tmp_path)
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15", min_days_between_assignments=4)
    variant = scheduler.generate_all_weekend_variants()[0].clone()
    variant.compute_unfillable_slots()
    before = variant.state.schedule.copy()

    assert "not independent" in unavailable_reason(variant)
    assert solve_weekdays_exactly(variant) is None
    pd.testing.assert_frame_equal(variant.state.schedule, before)


def test_without_or_tools_it_uses_the_search(monkeypatch):
    real_import = builtins.__import__

    def no_ortools(name, *args, **kwargs):
        if name.startswith("ortools"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ortools)
    variant = _small_two_week_variant()
    assert unavailable_reason(variant) == "OR-Tools is not installed"
    assert solve_weekdays_exactly(variant) is None


def test_too_many_fills_in_a_week_uses_the_search():
    variant = _small_two_week_variant()
    before = variant.state.schedule.copy()
    assert solve_weekdays_exactly(variant, max_fills_per_week=5) is None
    pd.testing.assert_frame_equal(variant.state.schedule, before)


# ── in the worker ──────────────────────────────────────────────────────────
def test_the_worker_uses_the_exact_solve_and_says_so(tmp_path):
    db = seed_db(tmp_path)
    variant = build_scheduler(db, "2026-11-02", "2026-11-15").generate_all_weekend_variants()[0]

    _idx, stats, _counts, _schedule = _evaluate_variant_core(
        (0, variant, WorkerTuningConfig()), with_profiling=False
    )

    assert stats["solver"] == "exact"
    assert stats["solver_optimal"] is True
    assert stats["gaps"] == 0


def test_the_exact_solve_is_never_worse_than_the_search(tmp_path):
    db = seed_db(tmp_path, time_off=[("C", "2026-11-04"), ("D", "2026-11-10")])
    variant = build_scheduler(db, "2026-11-02", "2026-11-15").generate_all_weekend_variants()[0]
    quick = WorkerTuningConfig(
        gap_fill_iterations=3,
        rebalance_iterations=3,
        window_refill_max_passes=2,
        window_refill_time_limit_ms=500,
        full_period_max_orders=2,
        full_period_per_attempt_time_ms=500,
    )

    def key(tuning):
        _i, _stats, _counts, schedule = _evaluate_variant_core(
            (0, variant, tuning), with_profiling=False
        )
        result = variant.clone()
        result.state.schedule.loc[schedule.index, ["main", "backup"]] = schedule[["main", "backup"]]
        result.recalculate_assignment_counts()
        quality = ScheduleQuality.from_variant(result)
        return balanced(
            (quality.total_gaps, quality.backup_spread, quality.main_spread, quality.total_spread)
        )

    assert key(quick) <= key(dataclasses.replace(quick, weekday_solver="search"))


def test_enumeration_stays_out_of_the_assignment_debug_log(monkeypatch):
    """The GUI turns the assignment debug logger on by default.

    Logging every enumeration probe made the solve run out of time there.
    """
    import scheduler.debug as debug

    class _CountingLogger:
        enabled = True

        def __init__(self):
            self.contexts = []

        def log(self, payload):
            self.contexts.append(payload.get("context"))

    variant = _small_two_week_variant()
    logger = _CountingLogger()
    monkeypatch.setattr(debug, "ASSIGNMENT_DEBUG_LOGGER", logger)

    outcome = solve_weekdays_exactly(variant)

    assert outcome is not None and outcome.optimal
    assert "gap_candidates" not in logger.contexts
