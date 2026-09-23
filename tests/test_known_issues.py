"""Audit findings that later phases of docs/scheduler-audit.md will fix.

Each test is a strict xfail: it documents the defect today and starts
failing (as an unexpected pass) the moment a fix lands, so the marker is
removed together with the bug.
"""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import REGULAR, build_scheduler, seed_db

from scheduler import WeekendHistory


class _TooManyOrderings(Exception):
    pass


def _count_orderings(variant, limit: int):
    """Wrap ``_assign_slot_sequence`` to count calls, stopping at ``limit``."""
    calls = [0]
    original = variant._assign_slot_sequence

    def counting(order, *args, **kwargs):
        calls[0] += 1
        if calls[0] > limit:
            raise _TooManyOrderings(calls[0])
        return original(order, *args, **kwargs)

    variant._assign_slot_sequence = counting
    return calls


@pytest.mark.xfail(strict=True, reason="audit #8: PRN nurses are never scheduled")
def test_a_prn_nurse_covers_a_day_no_regular_nurse_can(tmp_path):
    wednesday = "2026-11-04"
    db = seed_db(tmp_path, time_off=[(n, wednesday) for n in REGULAR])
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    assert variant.state.schedule.at[pd.Timestamp(wednesday), "main"] == "P"


@pytest.mark.xfail(strict=True, reason="audit #16: gap-fill orderings are uncapped")
def test_gap_fill_on_an_unfillable_week_stays_within_the_ordering_cap(tmp_path):
    wednesday = "2026-11-04"
    db = seed_db(tmp_path, time_off=[(n, wednesday) for n in REGULAR])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15")
    variant = scheduler.generate_all_weekend_variants()[0].clone()
    variant.assign_weekdays()

    cap = scheduler.config.max_week_permutations
    calls = _count_orderings(variant, limit=cap)
    # Raises _TooManyOrderings (failing the test) as soon as the cap is passed.
    variant._try_week_gap_permutations_no_revert(variant.get_weeks()[0])
    assert calls[0] <= cap


@pytest.mark.xfail(strict=True, reason="audit #18: capped orderings never vary the start")
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


@pytest.mark.xfail(strict=True, reason="audit #20: the streak compares calendar weeks")
def test_successive_repeat_violations_form_a_streak(tmp_path):
    db = seed_db(
        tmp_path,
        weekends=[
            ("2026-06-05", "A", "B"),
            ("2026-06-26", "A", "C"),
            ("2026-07-17", "A", "D"),
            ("2026-08-07", "A", "E"),
        ],
    )
    summary = WeekendHistory(db).get_violation_summary().set_index("nurse")
    assert summary.at["A", "total_viol"] == 3
    assert summary.at["A", "consec_viol"] == 3
