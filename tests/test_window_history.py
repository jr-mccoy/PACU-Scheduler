"""History at the window's edges must be read relative to the window.

docs/scheduler-audit.md findings 1 and 2: weekends already recorded inside
the window are the schedule being replaced, and weekends recorded after it
are fixed commitments the window's end has to respect.
"""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import WeekendHistory, WeekendPattern


def _record(db: str, tracking: dict) -> None:
    history = WeekendHistory(db)
    for friday, (fsf, sfs) in sorted(tracking.items()):
        history.add_assignment(friday, fsf, sfs)


# ── finding 1: regenerating an applied window ─────────────────────────────
@pytest.mark.xfail(strict=True, reason="audit #1: history inside the window is read")
def test_regenerating_an_applied_window_can_reproduce_it(tmp_path):
    db = seed_db(tmp_path)
    before = build_scheduler(
        db, "2026-11-02", "2026-11-08", max_weekend_variants=0
    ).generate_all_weekend_variants()
    applied = before[0].state.weekend_tracking
    _record(db, applied)

    again = build_scheduler(
        db, "2026-11-02", "2026-11-08", max_weekend_variants=0
    ).generate_all_weekend_variants()

    assert len(again) == len(before)
    assert any(v.state.weekend_tracking == applied for v in again)


@pytest.mark.xfail(strict=True, reason="audit #1: last pattern is read from the future")
def test_rotation_starts_from_the_last_pattern_before_the_window(tmp_path):
    db = seed_db(
        tmp_path,
        weekends=[
            ("2026-10-02", "A", "B"),  # before the window: A FSF, B SFS
            ("2026-11-06", "B", "A"),  # inside it: being replaced
        ],
    )
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29")

    assert scheduler.last_pattern["A"] == WeekendPattern.FSF
    assert scheduler.last_pattern["B"] == WeekendPattern.SFS


@pytest.mark.xfail(strict=True, reason="audit #1: weekends in the window count as history")
def test_a_replaced_weekend_does_not_block_its_neighbours(tmp_path):
    # A and B are recorded on Nov 6. Regenerating November must not treat
    # that as history that blocks them from Nov 13.
    db = seed_db(tmp_path, weekends=[("2026-11-06", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29")
    pre = scheduler._get_pre_scheduled_weekend_assignments()

    assert scheduler._check_weekend_gap_constraints(
        "A", pd.Timestamp("2026-11-13"), {}, scheduler.schedule, pre
    )


def test_a_manual_pattern_override_still_applies(tmp_path):
    db = seed_db(tmp_path, weekends=[("2026-10-02", "A", "B")])
    WeekendHistory(db).set_last_pattern("A", WeekendPattern.SFS)

    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29")

    assert scheduler.last_pattern["A"] == WeekendPattern.SFS


# ── finding 2: the window's end ───────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="audit #2: later recorded weekends are ignored")
@pytest.mark.parametrize("friday", ["2026-11-20", "2026-11-27"])  # 14 and 7 days before
def test_weekend_gap_respects_a_recorded_weekend_after_the_window(tmp_path, friday):
    db = seed_db(tmp_path, weekends=[("2026-12-04", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", weekend_gap_days=14)
    pre = scheduler._get_pre_scheduled_weekend_assignments()

    assert not scheduler._check_weekend_gap_constraints(
        "A", pd.Timestamp(friday), {}, scheduler.schedule, pre
    )


@pytest.mark.xfail(strict=True, reason="audit #2: weekday rules stop at end_date")
def test_weekdays_before_a_recorded_weekend_after_the_window_are_protected(tmp_path):
    # The window ends Thursday Dec 3; A works the weekend of Friday Dec 4.
    db = seed_db(tmp_path, weekends=[("2026-12-04", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-12-03")
    variant = scheduler.generate_all_weekend_variants()[0]

    for day in ("2026-12-01", "2026-12-02", "2026-12-03"):  # Tue–Thu
        for role in ("main", "backup"):
            eligible = variant._get_eligible_nurses_for_day(pd.Timestamp(day), role)
            assert "A" not in eligible, (day, role)
