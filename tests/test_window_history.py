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
from scheduler.engine import recorded_weekends_in_range


def _record(db: str, tracking: dict) -> None:
    history = WeekendHistory(db)
    for friday, (fsf, sfs) in sorted(tracking.items()):
        history.add_assignment(friday, fsf, sfs)


# ── finding 1: regenerating an applied window ─────────────────────────────
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


def test_an_override_does_not_outrank_weekends_recorded_in_the_window(tmp_path):
    # A worked FSF before the window and SFS inside it; the SFS override was
    # set after that and describes the replaced weekend, not the window start.
    db = seed_db(tmp_path, weekends=[("2026-10-02", "A", "B"), ("2026-11-06", "B", "A")])
    WeekendHistory(db).set_last_pattern("A", WeekendPattern.SFS)

    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29")

    assert scheduler.last_pattern["A"] == WeekendPattern.FSF


# ── finding 2: the window's end ───────────────────────────────────────────
@pytest.mark.parametrize(
    ("friday", "allowed"),
    [("2026-11-13", True), ("2026-11-20", True), ("2026-11-27", False)],  # 21, 14, 7 days
)
def test_weekend_gap_respects_a_recorded_weekend_after_the_window(tmp_path, friday, allowed):
    # The gap is inclusive: exactly weekend_gap_days apart is allowed.
    db = seed_db(tmp_path, weekends=[("2026-12-04", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", weekend_gap_days=14)
    pre = scheduler._get_pre_scheduled_weekend_assignments()

    assert (
        scheduler._check_weekend_gap_constraints(
            "A", pd.Timestamp(friday), {}, scheduler.schedule, pre
        )
        is allowed
    )


def test_weekdays_before_a_recorded_weekend_after_the_window_are_protected(tmp_path):
    # The window ends Thursday Dec 3; A works the weekend of Friday Dec 4.
    db = seed_db(tmp_path, weekends=[("2026-12-04", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-12-03")
    variant = scheduler.generate_all_weekend_variants()[0]

    for day in ("2026-12-01", "2026-12-02", "2026-12-03"):  # Tue–Thu
        for role in ("main", "backup"):
            eligible = variant._get_eligible_nurses_for_day(pd.Timestamp(day), role)
            assert "A" not in eligible, (day, role)


@pytest.mark.parametrize("source", ["history", "pre_scheduled"])
def test_spacing_respects_a_committed_shift_after_the_window(tmp_path, source):
    # The window ends Wednesday Dec 2; A already works Thursday Dec 3.
    db = seed_db(tmp_path, **{source: [("2026-12-03", "A", "B")]})
    scheduler = build_scheduler(db, "2026-11-02", "2026-12-02", min_days_between_assignments=2)
    variant = scheduler.generate_all_weekend_variants()[0]

    for day in ("2026-12-01", "2026-12-02"):
        assert "A" not in variant._get_eligible_nurses_for_day(pd.Timestamp(day), "main")


def test_recorded_weekends_in_range_lists_only_whole_weekends_inside(tmp_path):
    db = seed_db(
        tmp_path,
        weekends=[("2026-10-30", "A", "B"), ("2026-11-06", "C", "D"), ("2026-11-27", "E", "F")],
    )
    history = WeekendHistory(db)

    fridays = recorded_weekends_in_range(history, "2026-11-01", "2026-11-28")

    assert fridays == [pd.Timestamp("2026-11-06")]  # Oct 30 starts before, Nov 27 ends after
