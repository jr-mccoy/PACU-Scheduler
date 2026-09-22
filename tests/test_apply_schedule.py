"""Saving a chosen schedule records it completely (audit findings 4 and 22)."""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import WeekendHistory


def _chosen_schedule(db: str) -> pd.DataFrame:
    variant = build_scheduler(db, "2026-11-02", "2026-11-15").generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    return variant.state.schedule


@pytest.mark.xfail(strict=True, reason="audit #4: the CLI save never records new weekends")
def test_saving_from_the_cli_records_new_weekends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from cli.nurse_scheduler_ui import NurseSchedulerUI

    db = seed_db(tmp_path)
    schedule = _chosen_schedule(db)
    cli = NurseSchedulerUI(db)

    cli._save_selected_schedule(schedule, build_scheduler(db, "2026-11-02", "2026-11-15"))

    recorded = {friday: (fsf, sfs) for friday, fsf, sfs in WeekendHistory(db).get_assignments()}
    for friday in (pd.Timestamp("2026-11-06"), pd.Timestamp("2026-11-13")):
        assert recorded[friday] == (
            schedule.at[friday, "main"],
            schedule.at[friday, "backup"],
        )
