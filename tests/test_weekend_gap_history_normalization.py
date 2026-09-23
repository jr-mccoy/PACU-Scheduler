"""Regression test for the un-normalized backward weekend-gap history date.

The backward branch of ``_check_weekend_gap_constraints`` compared the raw
history date against the weekend under evaluation, while ``_weekend_gap_penalty``
snaps every history date to its Friday first. If a weekend ever landed in
history keyed by its Saturday/Sunday instead of its Friday, the hard gap
constraint read 1-2 days short and could wrongly block a legal gap.

The decision must be identical whether a given weekend is recorded as its
Friday or its Sunday.
"""

from __future__ import annotations

import pandas as pd

from scheduler import NurseScheduler, SchedulerConfig


class _DummyNurseManager:
    db_name = ":memory:"

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


class _DummyPreScheduler:
    def get_assignments_in_range(self, start_date, end_date):
        return {}


class _StubWeekendHistory:
    """History that returns a single configurable 'last weekend before'."""

    def __init__(self):
        self.last_weekend = None

    def get_last_weekend_before(self, nurse, before_date):
        if self.last_weekend is not None and self.last_weekend < before_date:
            return self.last_weekend
        return None

    def get_last_pattern(self, nurse):
        return None

    def get_weekends(self, nurse):
        return []

    def get_violation_counts(self):
        return {}


def _build_scheduler(history):
    return NurseScheduler(
        start_date="2026-01-05",
        end_date="2026-02-28",
        nurses=["Alice", "Bob"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=history,
        pre_scheduler=_DummyPreScheduler(),
        config=SchedulerConfig(weekend_gap_days=14),
    )


def test_backward_gap_invariant_to_friday_vs_sunday_history():
    history = _StubWeekendHistory()
    scheduler = _build_scheduler(history)

    # Prior weekend's Friday is Jan 2; its Sunday is Jan 4. The weekend under
    # evaluation sits so the Friday->Friday gap (16 days) is legal but the
    # raw-Sunday gap (14 days) reads as blocked with a 14-day minimum.
    friday = pd.Timestamp("2026-01-02")
    sunday = pd.Timestamp("2026-01-04")
    assert friday.weekday() == 4  # Friday
    assert sunday.weekday() == 6  # Sunday
    weekend = pd.Timestamp("2026-01-18")

    history.last_weekend = friday
    allowed_friday = scheduler._check_weekend_gap_constraints(
        "Alice", weekend, scheduler.schedule, {}
    )

    history.last_weekend = sunday
    allowed_sunday = scheduler._check_weekend_gap_constraints(
        "Alice", weekend, scheduler.schedule, {}
    )

    # A legal 16-day Friday->Friday gap must be allowed regardless of encoding.
    assert allowed_friday is True
    assert allowed_sunday == allowed_friday
