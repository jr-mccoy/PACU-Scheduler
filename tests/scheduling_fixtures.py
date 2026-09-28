"""Seed a throwaway scheduler database and build a scheduler against it.

The scheduler's history rules only show their behaviour against the real
repositories (weekend history, pre-schedule, per-day history), so the
regression tests for docs/scheduler-audit.md drive a real SQLite file rather
than stubs. Everything here is fictional single-letter staff.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from scheduler import (
    AssignmentHistory,
    NurseManager,
    NurseScheduler,
    PreScheduler,
    SchedulerConfig,
    ScheduleState,
    ScheduleVariant,
    WeekendHistory,
    ensure_schema,
)

# (name, is_prn, is_late_shift). Eight regular nurses keep strict rotation
# feasible for a month at a 14-day weekend gap; P is the PRN nurse.
ROSTER: tuple[tuple[str, bool, bool], ...] = (
    ("A", False, False),
    ("B", False, False),
    ("C", False, False),
    ("D", False, False),
    ("E", False, False),
    ("F", False, False),
    ("G", False, False),
    ("H", False, False),
    ("P", True, False),
)

REGULAR = tuple(name for name, is_prn, _ in ROSTER if not is_prn)


def seed_db(
    directory: Path,
    *,
    roster: Iterable[tuple[str, bool, bool]] = ROSTER,
    weekends: Iterable[tuple[str, str, str]] = (),
    time_off: Iterable[tuple[str, str]] = (),
    pre_scheduled: Iterable[tuple[str, str | None, str | None]] = (),
    history: Iterable[tuple[str, str | None, str | None]] = (),
    filename: str = "schedule.db",
) -> str:
    """Create ``directory/filename`` and return its path.

    ``weekends`` are ``(friday, fsf, sfs)`` rows of weekend history,
    ``time_off`` ``(nurse, date)`` pairs, and ``pre_scheduled`` and
    ``history`` ``(date, main, backup)`` rows.
    """
    path = str(Path(directory) / filename)
    ensure_schema(path)

    nurses = NurseManager(path)
    for name, is_prn, is_late in roster:
        nurses.add_nurse(name, is_prn=is_prn, is_late_shift=is_late)

    days_off: dict[str, set[pd.Timestamp]] = {}
    for nurse, day in time_off:
        days_off.setdefault(nurse, set()).add(pd.Timestamp(day))
    for nurse, days in days_off.items():
        nurses.update_unavailable_dates(nurse, days)

    weekend_history = WeekendHistory(path)
    for friday, fsf, sfs in weekends:
        weekend_history.add_assignment(pd.Timestamp(friday), fsf, sfs)

    pre_scheduler = PreScheduler(path)
    for day, main, backup in pre_scheduled:
        pre_scheduler.add_assignment(day, main, backup)

    assignment_history = AssignmentHistory(path)
    for day, main, backup in history:
        assignment_history.update_history(day, main, backup)

    return path


def build_scheduler(db: str, start: str, end: str, **config) -> NurseScheduler:
    """A scheduler over ``[start, end]`` reading every repository from ``db``.

    Keyword arguments go to :class:`SchedulerConfig`. The weekend gap defaults
    to 14 days so a month stays feasible for the small roster, and the beam
    to 25 variants so a month generates in about a second.
    """
    config.setdefault("weekend_gap_days", 14)
    config.setdefault("max_weekend_variants", 25)
    nurses = NurseManager(db)
    return NurseScheduler(
        pd.Timestamp(start),
        pd.Timestamp(end),
        nurses.get_non_prn_nurses(),
        nurses.get_prn_nurses(),
        nurses,
        WeekendHistory(db),
        PreScheduler(db),
        config=SchedulerConfig(**config),
    )


class _PlainNurses:
    """Nurse manager for variants built without a database: nobody is PRN or late."""

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False


def weekday_only_variant(
    nurses: list[str],
    weeks: int = 4,
    *,
    pre_scheduled: dict | None = None,
    start: str = "2026-01-05",
) -> ScheduleVariant:
    """A variant of Mon–Thu rows only, every nurse available, nothing assigned.

    ``start`` must be a Monday. With no weekends in it, only the weekday rules
    apply, which keeps exhaustive checks small. ``pre_scheduled`` maps days to
    ``{"main": name, "backup": name}`` pinned cells.
    """
    days = pd.DatetimeIndex(
        [
            pd.Timestamp(start) + pd.Timedelta(weeks=w, days=d)
            for w in range(weeks)
            for d in range(4)
        ]
    )
    schedule = pd.DataFrame(index=days, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = False
    counts = pd.Series(0, index=pd.Index(nurses))
    state = ScheduleState(schedule, counts, counts.copy(), {}, {})
    return ScheduleVariant(
        state,
        nurses,
        pd.DataFrame(True, index=days, columns=nurses),
        SchedulerConfig(),
        _PlainNurses(),
        pre_scheduled=pre_scheduled,
        console_debug=False,
    )


__all__ = ["REGULAR", "ROSTER", "build_scheduler", "seed_db", "weekday_only_variant"]
