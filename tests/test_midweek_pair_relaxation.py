"""Role-scoped one-day weekday gap relaxation (the midweek-pair settings).

``allow_one_day_weekday_gap`` relaxes spacing by one day for any roles;
``allow_midweek_pair_backup_only`` and ``allow_midweek_pair_mixed`` relax it
only for a Backup/Backup or a Main/Backup pair of shifts respectively.
"""

from __future__ import annotations

import pandas as pd
import pytest

from scheduler import NurseScheduler, SchedulerConfig
from scheduler.domain import ScheduleVariant
from scheduler.factory import build_scheduler_config_from_settings
from tests.test_pre_window_spacing import (
    _DummyNurseManager,
    _DummyPreScheduler,
    _StubAssignmentHistory,
    _WeekendHistoryWithTail,
)

MONDAY = pd.Timestamp("2026-01-12")
WEDNESDAY = pd.Timestamp("2026-01-14")


def _variant(**flags) -> ScheduleVariant:
    scheduler = NurseScheduler(
        start_date="2026-01-05",
        end_date="2026-01-18",
        nurses=["Alice", "Bob", "Cara", "Dan"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=_WeekendHistoryWithTail(),
        pre_scheduler=_DummyPreScheduler(),
        config=SchedulerConfig(min_days_between_assignments=2, **flags),
    )
    scheduler.assignment_history = _StubAssignmentHistory()
    return ScheduleVariant(
        scheduler.get_state_snapshot(),
        scheduler.nurses,
        scheduler.availability,
        scheduler.config,
        scheduler.nurse_manager,
        console_debug=False,
    )


def _cara_works_monday(variant: ScheduleVariant, role: str) -> ScheduleVariant:
    variant.state.schedule.at[MONDAY, role] = "Cara"
    return variant


def _wednesday_ok(variant: ScheduleVariant, role: str) -> bool:
    return variant._has_sufficient_spacing("Cara", WEDNESDAY, role, relaxed_spacing=True)


@pytest.mark.parametrize("monday_role", ["main", "backup"])
@pytest.mark.parametrize("wednesday_role", ["main", "backup"])
def test_no_relaxation_keeps_two_days_off(monday_role, wednesday_role):
    variant = _cara_works_monday(_variant(), monday_role)
    assert _wednesday_ok(variant, wednesday_role) is False


@pytest.mark.parametrize("monday_role", ["main", "backup"])
@pytest.mark.parametrize("wednesday_role", ["main", "backup"])
def test_master_flag_allows_any_roles(monday_role, wednesday_role):
    variant = _cara_works_monday(_variant(allow_one_day_weekday_gap=True), monday_role)
    assert _wednesday_ok(variant, wednesday_role) is True


@pytest.mark.parametrize(
    ("monday_role", "wednesday_role", "allowed"),
    [
        ("backup", "backup", True),
        ("main", "backup", False),
        ("backup", "main", False),
        ("main", "main", False),
    ],
)
def test_backup_only_allows_only_backup_pairs(monday_role, wednesday_role, allowed):
    variant = _cara_works_monday(_variant(allow_midweek_pair_backup_only=True), monday_role)
    assert _wednesday_ok(variant, wednesday_role) is allowed


@pytest.mark.parametrize(
    ("monday_role", "wednesday_role", "allowed"),
    [
        ("main", "backup", True),
        ("backup", "main", True),
        ("backup", "backup", False),
        ("main", "main", False),
    ],
)
def test_mixed_allows_only_main_backup_pairs(monday_role, wednesday_role, allowed):
    variant = _cara_works_monday(_variant(allow_midweek_pair_mixed=True), monday_role)
    assert _wednesday_ok(variant, wednesday_role) is allowed


def test_adjacent_day_is_never_relaxed():
    variant = _cara_works_monday(_variant(allow_one_day_weekday_gap=True), "backup")
    tuesday = pd.Timestamp("2026-01-13")
    assert variant._has_sufficient_spacing("Cara", tuesday, "backup", relaxed_spacing=True) is False


def test_unrelaxed_check_ignores_the_flags():
    variant = _cara_works_monday(_variant(allow_midweek_pair_backup_only=True), "backup")
    assert variant._has_sufficient_spacing("Cara", WEDNESDAY, "backup") is False


def test_any_flag_turns_the_fallback_on():
    assert not SchedulerConfig().one_day_gap_enabled
    assert SchedulerConfig(allow_one_day_weekday_gap=True).one_day_gap_enabled
    assert SchedulerConfig(allow_midweek_pair_backup_only=True).one_day_gap_enabled
    assert SchedulerConfig(allow_midweek_pair_mixed=True).one_day_gap_enabled


def test_settings_reach_the_scheduler_config():
    config = build_scheduler_config_from_settings(
        {
            "allow_one_day_weekday_gap": False,
            "allow_midweek_pair_backup_only": True,
            "allow_midweek_pair_mixed": False,
        }
    )
    assert config.allow_midweek_pair_backup_only is True
    assert config.allow_midweek_pair_mixed is False
    assert config.one_day_gap_enabled is True


def test_history_duration_reaches_assignment_history(monkeypatch):
    import scheduler.engine as engine

    seen = {}

    class _RecordingHistory:
        def __init__(self, db_name, history_duration_months=6):
            seen["months"] = history_duration_months

        def get_counts(self, start, end):
            return {}, {}

    monkeypatch.setattr(engine, "AssignmentHistory", _RecordingHistory)
    scheduler = NurseScheduler(
        start_date="2026-01-05",
        end_date="2026-01-18",
        nurses=["Alice", "Bob"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=_WeekendHistoryWithTail(),
        pre_scheduler=_DummyPreScheduler(),
        history_duration_months=3,
    )
    assert scheduler.history_duration_months == 3
    assert seen["months"] == 3
