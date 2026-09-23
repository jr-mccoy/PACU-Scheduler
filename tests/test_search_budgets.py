"""The weekday search spends its budget on slots it can fill.

Audit findings 16, 17 and 18: gap-fill tried every ordering of a week that
could not be filled completely, one impossible slot blocked rebalancing of
its whole week and window, and the capped rebalance only reshuffled the end
of the week.
"""

from __future__ import annotations

from math import factorial

import pandas as pd
from scheduling_fixtures import REGULAR, build_scheduler, seed_db

from scheduler import WorkerTuningConfig
from scheduler.evaluation.worker import _evaluate_variant_core

WEDNESDAY = pd.Timestamp("2026-11-04")


class _TooManyOrderings(Exception):
    pass


def _count_orderings(variant, limit: int, results: list | None = None):
    """Wrap ``_assign_slot_sequence`` to count calls, stopping past ``limit``."""
    calls = [0]
    original = variant._assign_slot_sequence

    def counting(order, *args, **kwargs):
        calls[0] += 1
        if calls[0] > limit:
            raise _TooManyOrderings(calls[0])
        ok = original(order, *args, **kwargs)
        if results is not None:
            results.append(ok)
        return ok

    variant._assign_slot_sequence = counting
    return calls


def _variant_with_nobody_on_wednesday(tmp_path, end="2026-11-15"):
    db = seed_db(tmp_path, time_off=[(n, WEDNESDAY) for n in REGULAR])
    scheduler = build_scheduler(db, "2026-11-02", end)
    return scheduler, scheduler.generate_all_weekend_variants()[0].clone()


def test_slots_nobody_can_take_are_found(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path)

    unfillable = variant.compute_unfillable_slots()

    assert unfillable == {(WEDNESDAY, "main"), (WEDNESDAY, "backup")}
    assert variant.clone().unfillable_slots == unfillable


def test_unfillable_slots_are_not_searched_for(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path)
    variant.compute_unfillable_slots()
    week = variant.get_weeks()[0]

    assert (WEDNESDAY, "main") not in variant._build_window_varlist(week)
    assert variant._count_fillable_weekday_gaps() == variant.count_gaps() - 2


def test_gap_fill_on_an_unfillable_week_stays_within_the_ordering_cap(tmp_path):
    # Without the precomputed unfillable slots, the complete-fill search fails
    # and the fallback must stop at max_week_permutations, not try all 8!.
    scheduler, variant = _variant_with_nobody_on_wednesday(tmp_path)
    variant.assign_weekdays()

    cap = scheduler.config.max_week_permutations
    calls = _count_orderings(variant, limit=cap)
    variant._try_week_gap_permutations_no_revert(variant.get_weeks()[0])
    assert calls[0] <= cap


def test_a_week_with_an_unfillable_slot_can_still_be_rebalanced(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path)
    variant.compute_unfillable_slots()
    variant.assign_weekdays()

    results: list[bool] = []
    _count_orderings(variant, limit=10_000, results=results)
    variant._try_week_permutations_no_revert(variant.get_weeks()[0])

    # Before, every ordering failed on the impossible Wednesday slots.
    assert any(results)


def test_evaluating_a_variant_reports_its_unfillable_slots(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path)
    quick = WorkerTuningConfig(
        gap_fill_iterations=3,
        rebalance_iterations=3,
        window_refill_max_passes=2,
        window_refill_time_limit_ms=500,
        full_period_max_orders=2,
        full_period_per_attempt_time_ms=500,
    )

    _idx, stats, _counts, schedule = _evaluate_variant_core(
        (0, variant, quick), with_profiling=False
    )

    assert stats["unfillable"] == 2
    assert stats["unfillable_slots"] == ["2026-11-04 backup", "2026-11-04 main"]
    assert stats["gaps"] == 2  # everything else was filled
    assert schedule.loc[WEDNESDAY, ["main", "backup"]].isna().all()


# ── capped orderings ──────────────────────────────────────────────────────
def test_small_slot_sets_are_searched_exhaustively(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path, end="2026-11-08")
    orders = list(variant.slot_orderings("abcd", limit=200, seed=1))
    assert len(orders) == factorial(4) == len(set(orders))


def test_capped_orderings_are_distinct_deterministic_and_start_as_given(tmp_path):
    _, variant = _variant_with_nobody_on_wednesday(tmp_path, end="2026-11-08")
    slots = tuple(range(8))

    first = list(variant.slot_orderings(slots, limit=200, seed=7))
    again = list(variant.slot_orderings(slots, limit=200, seed=7))

    assert first == again
    assert first[0] == slots
    assert len(first) == len(set(first)) == 200
    assert {order[0] for order in first} == set(slots)  # every slot gets to go first


def test_capped_rebalance_varies_the_first_slot(tmp_path):
    db = seed_db(tmp_path)
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    variant.assign_weekdays()

    orders: list[tuple] = []
    original = variant._assign_slot_sequence

    def never_better(order, *args, **kwargs):
        # Record every ordering tried and reject it, so the whole capped
        # sample is drawn instead of stopping at the first improvement.
        orders.append(tuple(order))
        original(order, *args, **kwargs)
        return False

    variant._assign_slot_sequence = never_better
    variant._try_week_permutations_no_revert(variant.get_weeks()[0])
    assert len({order[0] for order in orders}) > 1


def test_the_gap_report_lists_unfillable_slots(tmp_path):
    from scheduler.exporters import write_gap_report

    stats = {
        "early_gaps": 6,
        "gaps": 2,
        "unfillable": 2,
        "unfillable_slots": ["2026-11-04 backup", "2026-11-04 main"],
    }
    path = write_gap_report([(0, stats, {}, None)], tmp_path / "gaps.txt")

    text = open(path, encoding="utf-8").read()
    assert "Unfillable" in text
    assert "       1         6         2         4           2" in text
    assert "Variant 1: 2026-11-04 backup, 2026-11-04 main" in text
