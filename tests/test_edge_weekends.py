"""Weekends cut by the window's edges are scheduled whole (audit finding 5)."""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler.engine import whole_weekend_range
from scheduler.runtime import is_empty

T = pd.Timestamp


def _cells(variant, day: str) -> tuple:
    schedule = variant.state.schedule
    return schedule.at[T(day), "main"], schedule.at[T(day), "backup"]


@pytest.mark.parametrize(
    ("requested", "recorded", "expected"),
    [
        (("2026-10-05", "2026-11-01"), None, ("2026-10-05", "2026-11-01")),  # Mon..Sun: as is
        (("2026-10-03", "2026-10-30"), None, ("2026-10-02", "2026-11-01")),  # Sat..Fri
        (("2026-10-04", "2026-10-31"), None, ("2026-10-02", "2026-11-01")),  # Sun..Sat
        (("2026-10-03", "2026-10-30"), "2026-10-02", ("2026-10-03", "2026-11-01")),
        (("2026-10-03", "2026-10-30"), "2026-10-30", ("2026-10-02", "2026-10-30")),
    ],
)
def test_whole_weekend_range(requested, recorded, expected):
    fridays = {T(recorded)} if recorded else set()
    got = whole_weekend_range(T(requested[0]), T(requested[1]), is_recorded=fridays.__contains__)
    assert got == (T(expected[0]), T(expected[1]))


def test_a_saturday_to_friday_range_leaves_no_weekend_day_blank(tmp_path):
    db = seed_db(tmp_path)
    scheduler = build_scheduler(db, "2026-10-03", "2026-10-30")  # Sat .. Fri

    variant = scheduler.generate_all_weekend_variants()[0]

    assert (scheduler.start_date, scheduler.end_date) == (T("2026-10-02"), T("2026-11-01"))
    assert (scheduler.requested_start_date, scheduler.requested_end_date) == (
        T("2026-10-03"),
        T("2026-10-30"),
    )
    for day in ("2026-10-02", "2026-10-03", "2026-10-04", "2026-10-30", "2026-10-31"):
        main, backup = _cells(variant, day)
        assert not is_empty(main) and not is_empty(backup), day


def test_a_recorded_weekend_at_the_start_is_kept_not_regenerated(tmp_path):
    # Last month's run recorded the weekend of Oct 2; this range starts Oct 3.
    db = seed_db(tmp_path, weekends=[("2026-10-02", "A", "B")])
    scheduler = build_scheduler(db, "2026-10-03", "2026-10-30")

    assert scheduler.start_date == T("2026-10-03")
    for variant in scheduler.generate_all_weekend_variants():
        assert _cells(variant, "2026-10-03") == ("B", "A")  # Sat: SFS main
        assert _cells(variant, "2026-10-04") == ("A", "B")  # Sun: FSF main


def test_a_recorded_weekend_at_the_end_is_kept_not_regenerated(tmp_path):
    db = seed_db(tmp_path, weekends=[("2026-10-30", "C", "D")])
    scheduler = build_scheduler(db, "2026-10-05", "2026-10-31")  # Mon .. Sat

    assert scheduler.end_date == T("2026-10-31")
    for variant in scheduler.generate_all_weekend_variants():
        assert _cells(variant, "2026-10-30") == ("C", "D")
        assert _cells(variant, "2026-10-31") == ("D", "C")
