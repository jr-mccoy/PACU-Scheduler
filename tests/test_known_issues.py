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


@pytest.mark.xfail(strict=True, reason="audit #8: PRN nurses are never scheduled")
def test_a_prn_nurse_covers_a_day_no_regular_nurse_can(tmp_path):
    wednesday = "2026-11-04"
    db = seed_db(tmp_path, time_off=[(n, wednesday) for n in REGULAR])
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    assert variant.state.schedule.at[pd.Timestamp(wednesday), "main"] == "P"


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
