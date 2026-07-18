"""Regression test for the stale weekday/total tie-breaker caches.

`_assign_roles_for_date` writes the picked nurse directly into the schedule
and bumps the role count without invalidating the weekday/total caches that
`_select_best_candidate`'s tier-1.5 and tier-2 tie-breakers read. This test
asserts the caches agree with freshly recomputed ground truth at every pick
during `assign_weekdays()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from scheduler import ScheduleState, ScheduleVariant, SchedulerConfig


class _DummyNurseManager:
    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


def _weekday_mon_thu_dates(weeks: int, start: str = "2026-01-05") -> pd.DatetimeIndex:
    base = pd.Timestamp(start)  # a Monday
    dates = []
    for w in range(weeks):
        week_start = base + pd.Timedelta(weeks=w)
        for d in range(4):  # Mon-Thu
            dates.append(week_start + pd.Timedelta(days=d))
    return pd.DatetimeIndex(dates)


def _build_weekday_variant(nurses, weeks: int = 4) -> ScheduleVariant:
    idx = _weekday_mon_thu_dates(weeks=weeks)
    schedule = pd.DataFrame(index=idx, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = False
    counts_idx = pd.Index(list(nurses))
    state = ScheduleState(
        schedule=schedule,
        main_assignment_counts=pd.Series(0, index=counts_idx),
        backup_assignment_counts=pd.Series(0, index=counts_idx),
        last_assignment={n: None for n in nurses},
        last_pattern={n: None for n in nurses},
        weekend_tracking={},
        nurse_weekend_lists={n: [] for n in nurses},
        rotation_repeats=0,
    )
    availability = pd.DataFrame(True, index=idx, columns=list(nurses))
    return ScheduleVariant(
        state=state,
        nurses=list(nurses),
        availability=availability,
        config=SchedulerConfig(),
        nurse_manager=_DummyNurseManager(),
        pre_scheduled={},
        console_debug=False,
    )


def _ground_truth_total(variant: ScheduleVariant) -> dict:
    total = variant.state.main_assignment_counts + variant.state.backup_assignment_counts
    return total.to_dict()


def _ground_truth_weekday(variant: ScheduleVariant, weekday: int) -> dict:
    """Recompute per-nurse weekday counts the same way _weekday_counts_for does,
    but always from the live schedule (never the cache)."""
    sched = variant.state.schedule
    sub = sched.loc[~sched["is_weekend"], ["main", "backup"]]
    sub = sub[sub.index.weekday == weekday]
    if sub.empty:
        return {}
    m = sub["main"].value_counts()
    b = sub["backup"].value_counts()
    counts = m.add(b, fill_value=0).astype(int)
    return counts.to_dict()


def test_weekday_caches_stay_consistent_during_assign_weekdays():
    nurses = ["N1", "N2", "N3", "N4", "N5", "N6"]
    variant = _build_weekday_variant(nurses, weeks=4)

    original = variant._select_best_candidate
    mismatches = []
    picks = {"count": 0}

    def checked_select(eligible, role, date=None):
        picks["count"] += 1
        # Right after the previous pick's mutation the caches that the
        # tie-breakers consult here must already agree with ground truth.
        if variant._get_total_counts().to_dict() != _ground_truth_total(variant):
            mismatches.append(("total", date, role))
        if date is not None:
            wd = date.weekday()
            if wd in (0, 1, 2, 3) and (
                variant._weekday_counts_for(wd) != _ground_truth_weekday(variant, wd)
            ):
                mismatches.append(("weekday", date, role))
        return original(eligible, role, date)

    variant._select_best_candidate = checked_select
    variant.assign_weekdays()

    assert picks["count"] > 0, "no picks were made — test did not exercise the caches"
    assert mismatches == [], f"stale cache reads during assign_weekdays(): {mismatches}"
