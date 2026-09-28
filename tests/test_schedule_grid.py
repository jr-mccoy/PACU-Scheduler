"""Step 3 of docs/scheduler-optimization-audit.md (findings 3 and 4).

The weekday search reads and writes a :class:`ScheduleGrid` of plain lists
instead of the schedule frame. These tests pin the hand-over between the
two, and check the rewritten rules against the frame-based versions they
replaced, on schedules with weekends, pinned cells and shifts outside the
window.
"""

from __future__ import annotations

import pickle
import random
from datetime import timedelta

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db, weekday_only_variant

NURSES = ["N1", "N2", "N3", "N4", "N5", "N6"]
MONDAY = pd.Timestamp("2026-01-05")


# ── the hand-over between grid and frame ───────────────────────────────────
def test_grid_writes_reach_the_frame_and_frame_writes_reach_the_grid():
    variant = weekday_only_variant(NURSES, weeks=1)
    state = variant.state

    state.grid().set(MONDAY, "main", "N1")
    assert state.schedule.at[MONDAY, "main"] == "N1"

    state.schedule.at[MONDAY, "backup"] = "N2"  # the grid was handed back
    assert state.grid().get(MONDAY, "backup") == "N2"
    assert state.grid().get(MONDAY, "main") == "N1"


def test_clones_and_pickles_keep_cells_written_only_to_the_grid():
    variant = weekday_only_variant(NURSES, weeks=1)
    variant.state.grid().set(MONDAY, "main", "N3")

    assert variant.clone().state.schedule.at[MONDAY, "main"] == "N3"
    restored = pickle.loads(pickle.dumps(variant))
    assert restored.state.schedule.at[MONDAY, "main"] == "N3"
    assert restored.is_slot_empty(MONDAY, "backup")


def test_offsets_use_calendar_days_even_where_the_index_skips_them():
    variant = weekday_only_variant(NURSES, weeks=2)  # Mon–Thu rows only
    state = variant.state
    thursday = state.positions()[MONDAY + timedelta(days=3)]

    assert state.offset(thursday, 1) == (None, MONDAY + timedelta(days=4))  # Friday: no row
    assert state.offset(thursday, 4) == (
        state.positions()[MONDAY + timedelta(days=7)],
        MONDAY + timedelta(days=7),
    )


# ── the rewritten rules against the frame-based ones ───────────────────────
def _spacing_by_frame(variant, nurse, date, role, *, relaxed_spacing):
    """``_has_sufficient_spacing`` as it read the frame before step 3."""
    sched = variant.state.schedule
    base = int(variant.config.min_days_between_assignments)
    min_days_off = base
    relaxed = (
        relaxed_spacing
        and variant.config.one_day_gap_enabled
        and variant._weekday_relaxation_applicable(nurse, date)
    )
    if relaxed:
        min_days_off = max(1, base - 1)
    idx_set = frozenset(sched.index)
    outside = variant._worked_outside_window(nurse)
    for offset in range(1, min_days_off + 1):
        for check in (date - timedelta(days=offset), date + timedelta(days=offset)):
            if check in idx_set:
                if nurse == sched.at[check, "main"] or nurse == sched.at[check, "backup"]:
                    return False
            elif check in outside:
                return False
    if relaxed and min_days_off < base and not variant.config.allow_one_day_weekday_gap:
        for offset in range(min_days_off + 1, base + 1):
            for check in (date - timedelta(days=offset), date + timedelta(days=offset)):
                if check in idx_set:
                    other = (
                        "main"
                        if sched.at[check, "main"] == nurse
                        else "backup"
                        if sched.at[check, "backup"] == nurse
                        else None
                    )
                    if other and not variant.config.one_day_gap_allows_roles(role, other):
                        return False
                elif check in outside:
                    return False
    return True


def _weekly_by_frame(variant, nurse, date, role):
    """``_validate_weekly_assignment_limits`` as it read the frame before step 3."""
    sched = variant.state.schedule
    week_start = date - timedelta(days=date.weekday())
    mains = backups = 0
    for i in range(4):
        d = week_start + timedelta(days=i)
        if d == date or d not in sched.index:
            continue
        mains += sched.at[d, "main"] == nurse
        backups += sched.at[d, "backup"] == nurse
    if role == "main" and mains >= 1:
        return False
    return mains + backups < 2


@pytest.mark.parametrize(
    "config",
    [{}, {"allow_one_day_weekday_gap": True}, {"allow_midweek_pair_mixed": True}],
    ids=["default", "one-day-gap", "midweek-pairs"],
)
def test_grid_rules_match_the_frame_rules_on_random_schedules(tmp_path, config):
    db = seed_db(
        tmp_path,
        pre_scheduled=[("2026-11-10", "A", None)],
        history=[("2026-11-01", "B", "C"), ("2026-10-31", "D", "E")],
    )
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", **config)
    variant = scheduler.generate_all_weekend_variants()[0].clone()
    weekdays = variant.get_weekdays()
    rng = random.Random(7)

    checked = 0
    for _ in range(5):
        # Scatter nurses over the weekdays, ignoring the rules: the checks
        # must agree on any schedule, not only on legal ones.
        variant._clear_week_assignments(weekdays)
        for day in weekdays:
            for role in ("main", "backup"):
                if not variant._is_pre_scheduled(day, role) and rng.random() < 0.6:
                    variant._place_assignment(day, role, rng.choice(variant.nurses))
        for day in weekdays:
            for role in ("main", "backup"):
                for nurse in variant.nurses:
                    for relaxed in (False, True):
                        assert variant._has_sufficient_spacing(
                            nurse, day, role, relaxed_spacing=relaxed
                        ) == _spacing_by_frame(variant, nurse, day, role, relaxed_spacing=relaxed)
                    assert variant._validate_weekly_assignment_limits(
                        nurse, day, role
                    ) == _weekly_by_frame(variant, nurse, day, role)
                    checked += 1
    assert checked > 1000
