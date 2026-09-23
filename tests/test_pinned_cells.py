"""Pinned (pre-scheduled) cells: consistent weekend handling and a pre-flight check.

Audit finding 11: a Friday pin fixed the weekend's pattern nurse and bypassed
their checks, while an equivalent Saturday or Sunday pin still had to pass
every check, so it could make the weekend infeasible. Pinned cells were
never validated.
"""

from __future__ import annotations

import pandas as pd
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import NurseManager, WeekendPattern

FRI, SAT, SUN = "2026-11-06", "2026-11-07", "2026-11-08"


def _window(db):
    return build_scheduler(db, "2026-11-02", "2026-11-08")


def test_a_saturday_pin_fixes_the_pattern_nurse_like_a_friday_pin(tmp_path):
    # C last worked SFS, so strict rotation would only let C work FSF next.
    # Pinning C as Saturday Main (an SFS cell) must still be honoured.
    db = seed_db(
        tmp_path,
        weekends=[("2026-10-02", "D", "C")],
        pre_scheduled=[(SAT, "C", None)],
    )
    scheduler = _window(db)

    pinned = scheduler._get_pre_scheduled_weekend_assignments()[pd.Timestamp(FRI)]
    assert pinned == {WeekendPattern.FSF: None, WeekendPattern.SFS: "C"}

    variants = scheduler.generate_all_weekend_variants()
    assert variants
    assert all(v.state.weekend_tracking[pd.Timestamp(FRI)][1] == "C" for v in variants)


def test_contradictory_weekend_pins_are_reported_and_block_the_weekend(tmp_path):
    # Friday Main and Sunday Main are both FSF cells, pinned to different nurses.
    db = seed_db(tmp_path, pre_scheduled=[(FRI, "A", None), (SUN, "B", None)])
    scheduler = _window(db)

    issues = scheduler.validate_pre_schedule()

    assert [issue.day for issue in issues] == [pd.Timestamp(FRI)]
    assert "FSF cells name A, B" in issues[0].message
    assert scheduler.generate_all_weekend_variants() == []


def test_the_same_nurse_on_both_patterns_is_a_contradiction(tmp_path):
    db = seed_db(tmp_path, pre_scheduled=[(FRI, "A", None), (SAT, "A", None)])
    [issue] = _window(db).validate_pre_schedule()
    assert "A pinned to both patterns" in issue.message


def test_the_pre_flight_check_finds_each_kind_of_bad_pin(tmp_path):
    roster = [
        ("A", False, False),
        ("B", False, False),
        ("C", False, True),
        ("D", False, True),
        ("E", False, False),
        ("F", False, False),
    ]
    db = seed_db(
        tmp_path,
        roster=roster,
        time_off=[("B", "2026-11-03")],
        pre_scheduled=[
            ("2026-11-02", "A", "A"),  # both roles
            ("2026-11-03", "B", None),  # B is off that day
            ("2026-11-04", "C", "D"),  # two late-shift nurses
            ("2026-11-05", "E", None),  # E is deactivated below
        ],
    )
    NurseManager(db).deactivate_nurse("E")

    messages = [issue.message for issue in _window(db).validate_pre_schedule()]

    assert messages == [
        "Mon Nov 02: A is pinned as both Main and Backup.",
        "Tue Nov 03: Main B has that day off.",
        "Wed Nov 04: C and D are both late-shift nurses.",
        "Thu Nov 05: Main E is not an active nurse.",
    ]


def test_a_clean_pre_schedule_has_no_issues(tmp_path):
    db = seed_db(tmp_path, pre_scheduled=[(FRI, "A", "B"), ("2026-11-03", "C", "D")])
    assert _window(db).validate_pre_schedule() == []
