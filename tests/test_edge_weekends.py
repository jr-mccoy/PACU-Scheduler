"""Weekends cut by the window's edges are scheduled whole (audit finding 5)."""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler.runtime import is_empty


def _cells(variant, day: str) -> tuple:
    schedule = variant.state.schedule
    return schedule.at[pd.Timestamp(day), "main"], schedule.at[pd.Timestamp(day), "backup"]


@pytest.mark.xfail(strict=True, reason="audit #5: edge weekend days are left blank")
def test_a_saturday_to_friday_range_leaves_no_weekend_day_blank(tmp_path):
    db = seed_db(tmp_path)
    scheduler = build_scheduler(db, "2026-10-03", "2026-10-30")  # Sat .. Fri

    variant = scheduler.generate_all_weekend_variants()[0]

    for day in ("2026-10-03", "2026-10-04", "2026-10-30"):
        main, backup = _cells(variant, day)
        assert not is_empty(main) and not is_empty(backup), day
