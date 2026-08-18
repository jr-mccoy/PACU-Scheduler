"""Tests for pre-window history visibility in the weekday spacing constraint.

The in-window spacing check (`_has_sufficient_spacing`) can only see schedule
cells inside the scheduling window, so historically a shift worked the day
before ``start_date`` was invisible to ``min_days_between_assignments``. The
scheduler now seeds each state snapshot with the days every nurse worked in
the lookback window immediately before the start date — from weekend history
and from the persisted per-day schedule history — and the spacing check
consults them for dates that fall outside the schedule index.
"""

from __future__ import annotations

import pandas as pd

from scheduler import NurseScheduler, SchedulerConfig
from scheduler.domain import ScheduleVariant


class _DummyNurseManager:
    db_name = ":memory:"

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse):
        return []


class _DummyPreScheduler:
    def get_assignments_in_range(self, start_date, end_date):
        return {}


class _WeekendHistoryWithTail:
    """Alice worked the weekend of Fri 2026-01-02 .. Sun 2026-01-04."""

    def get_last_weekend_before(self, nurse, before_date):
        if nurse == "Alice":
            return pd.Timestamp("2026-01-02")
        return None

    def get_last_pattern(self, nurse):
        return None

    def get_weekends(self, nurse):
        if nurse == "Alice":
            return [pd.Timestamp("2026-01-02")]
        return []

    def get_violation_counts(self):
        return {}


class _StubAssignmentHistory:
    """Bob worked a weekday shift on Sun 2026-01-04 (day before the window)."""

    def get_history(self, start_date=None, end_date=None):
        return [("2026-01-04", "Bob", None)]


def _build_scheduler() -> NurseScheduler:
    scheduler = NurseScheduler(
        start_date="2026-01-05",  # Monday
        end_date="2026-01-18",
        nurses=["Alice", "Bob", "Cara", "Dan"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=_WeekendHistoryWithTail(),
        pre_scheduler=_DummyPreScheduler(),
        config=SchedulerConfig(min_days_between_assignments=2),
    )
    scheduler.assignment_history = _StubAssignmentHistory()
    return scheduler


def _build_variant(scheduler: NurseScheduler) -> ScheduleVariant:
    return ScheduleVariant(
        scheduler.get_state_snapshot(),
        scheduler.nurses,
        scheduler.availability,
        scheduler.config,
        scheduler.nurse_manager,
        console_debug=False,
    )


def test_snapshot_seeds_pre_window_worked_days():
    state = _build_scheduler().get_state_snapshot()

    # Only days inside the 2-day lookback window (Jan 3–4) are recorded.
    assert state.pre_window_worked["Alice"] == frozenset(
        {pd.Timestamp("2026-01-03"), pd.Timestamp("2026-01-04")}
    )
    # Bob's day comes from the per-day schedule history (a main shift).
    assert state.pre_window_worked["Bob"] == frozenset({pd.Timestamp("2026-01-04")})
    # Nurses with no recent work carry no entry at all.
    assert "Cara" not in state.pre_window_worked


def test_spacing_sees_shift_worked_before_window_start():
    variant = _build_variant(_build_scheduler())
    monday = pd.Timestamp("2026-01-05")
    tuesday = pd.Timestamp("2026-01-06")
    wednesday = pd.Timestamp("2026-01-07")

    # Alice worked Sun Jan 4: Monday (1 day) and Tuesday (2 days) both violate
    # min_days_between_assignments=2; Wednesday is clear.
    assert variant._has_sufficient_spacing("Alice", monday, "main") is False
    assert variant._has_sufficient_spacing("Alice", tuesday, "main") is False
    assert variant._has_sufficient_spacing("Alice", wednesday, "main") is True

    # Same for Bob's weekday shift from the per-day history.
    assert variant._has_sufficient_spacing("Bob", monday, "main") is False

    # A nurse with no pre-window work is unaffected.
    assert variant._has_sufficient_spacing("Cara", monday, "main") is True


def test_pre_window_worked_survives_cloning():
    variant = _build_variant(_build_scheduler())
    clone = variant.clone()
    assert clone.state.pre_window_worked == variant.state.pre_window_worked
    assert clone._has_sufficient_spacing("Alice", pd.Timestamp("2026-01-05"), "main") is False
