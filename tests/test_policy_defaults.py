"""Scheduling policies as decided: shared defaults, the weekend gap, PRN nurses.

Audit findings 8 and 12.
"""

from __future__ import annotations

import pandas as pd
from scheduling_fixtures import REGULAR, build_scheduler, seed_db

from scheduler import SchedulerConfig, SharedSettings


def test_scheduler_config_defaults_match_the_shared_settings():
    config = SchedulerConfig()
    for key, default in SharedSettings.DEFAULTS.items():
        if hasattr(config, key) and key != "scoring_weights":
            assert getattr(config, key) == default, key
    expected = SharedSettings.DEFAULTS["scoring_weights"]
    total = sum(expected.values())
    assert config.scoring_weights == {k: v / total for k, v in expected.items()}


def _allowed(tmp_path, gap_days: int, friday: str) -> bool:
    # A last worked the weekend of Friday Oct 9.
    db = seed_db(tmp_path, weekends=[("2026-10-09", "A", "B")])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", weekend_gap_days=gap_days)
    pre = scheduler._get_pre_scheduled_weekend_assignments()
    return scheduler._check_weekend_gap_constraints(
        "A", pd.Timestamp(friday), scheduler.schedule, pre
    )


def test_weekends_exactly_the_gap_apart_are_allowed(tmp_path):
    assert _allowed(tmp_path, 28, "2026-11-06")  # exactly 28 days after Oct 9


def test_weekends_closer_than_the_gap_are_not(tmp_path):
    assert not _allowed(tmp_path, 35, "2026-11-06")  # 28 days, five weeks required


def test_prn_nurses_are_only_scheduled_when_pinned(tmp_path):
    # Audit finding 8, decided: PRN nurses stay out of automatic scheduling,
    # even for a day no regular nurse can take. Managers pin them by hand.
    wednesday = pd.Timestamp("2026-11-04")
    db = seed_db(tmp_path, time_off=[(n, wednesday) for n in REGULAR])
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    variant.assign_weekdays()

    assert "P" not in variant.nurses
    assert not (variant.state.schedule[["main", "backup"]] == "P").any().any()


def test_a_pinned_prn_nurse_is_kept(tmp_path):
    db = seed_db(tmp_path, pre_scheduled=[("2026-11-04", "P", None)])
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    assert variant.state.schedule.at[pd.Timestamp("2026-11-04"), "main"] == "P"
