"""Saving a chosen schedule records it completely (audit findings 4 and 22)."""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import AssignmentHistory, WeekendHistory, apply_schedule

FRIDAYS = (pd.Timestamp("2026-11-06"), pd.Timestamp("2026-11-13"))


def _chosen_schedule(db: str) -> pd.DataFrame:
    variant = build_scheduler(db, "2026-11-02", "2026-11-15").generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    return variant.state.schedule


def _weekends(db: str) -> dict:
    return {friday: (fsf, sfs) for friday, fsf, sfs in WeekendHistory(db).get_assignments()}


def _days(db: str) -> dict:
    return {
        pd.Timestamp(day): (main, backup)
        for day, main, backup in AssignmentHistory(db).get_history("2026-11-02", "2026-11-15")
    }


def test_saving_from_the_cli_records_new_weekends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from cli.nurse_scheduler_ui import NurseSchedulerUI

    db = seed_db(tmp_path)
    schedule = _chosen_schedule(db)

    NurseSchedulerUI(db)._save_selected_schedule(schedule)

    recorded = _weekends(db)
    for friday in FRIDAYS:
        assert recorded[friday] == (schedule.at[friday, "main"], schedule.at[friday, "backup"])


def test_apply_records_every_day_and_weekend(tmp_path):
    db = seed_db(tmp_path)
    schedule = _chosen_schedule(db)

    report = apply_schedule(db, schedule)

    days = _days(db)
    filled = [d for d in schedule.index if schedule.at[d, "main"] or schedule.at[d, "backup"]]
    assert report.days_recorded == len(filled) == len(days)
    for day in filled:
        assert days[day] == (schedule.at[day, "main"], schedule.at[day, "backup"])
    assert report.weekends_recorded == 2
    assert set(_weekends(db)) == set(FRIDAYS)


def test_applying_again_replaces_the_earlier_schedule(tmp_path):
    db = seed_db(tmp_path)
    first = _chosen_schedule(db)
    apply_schedule(db, first)

    second = first.copy()
    second.loc[FRIDAYS[1] : FRIDAYS[1] + pd.Timedelta(days=2), ["main", "backup"]] = None
    second.loc[pd.Timestamp("2026-11-03"), ["main", "backup"]] = None
    report = apply_schedule(db, second)

    assert FRIDAYS[1] not in _weekends(db)  # no pair any more: not left behind
    assert pd.Timestamp("2026-11-03") not in _days(db)
    assert report.weekends_removed == 1 and report.days_cleared == 4


def test_apply_writes_nothing_when_a_name_is_unknown(tmp_path):
    db = seed_db(tmp_path)
    schedule = _chosen_schedule(db)
    schedule.at[pd.Timestamp("2026-11-03"), "main"] = "Nobody"

    with pytest.raises(ValueError, match="Nobody"):
        apply_schedule(db, schedule)

    assert _days(db) == {} and _weekends(db) == {}


def test_apply_is_all_or_nothing(tmp_path, monkeypatch):
    db = seed_db(tmp_path)
    schedule = _chosen_schedule(db)

    def fail(self, conn, chronological):
        raise RuntimeError("simulated failure after the writes")

    monkeypatch.setattr(WeekendHistory, "_rebuild_violation_tables", fail)

    with pytest.raises(RuntimeError):
        apply_schedule(db, schedule)

    assert _days(db) == {} and _weekends(db) == {}
