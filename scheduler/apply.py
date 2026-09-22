"""Record a chosen schedule in assignment and weekend history, all at once.

Applying used to be two separate loops, one in the GUI's review dialog and
one in the CLI. Each wrote one transaction per day and rebuilt the rotation
tables once per weekend, and the CLI's copy wrote new weekends with an SQL
``UPDATE`` that matched no row, so it never recorded them. This module is the
single path both front ends use. A schedule is written in one transaction
with one rotation rebuild, so a failure leaves history exactly as it was.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from .repositories import WeekendHistory
from .runtime import is_empty

FRIDAY = 4


@dataclass(frozen=True)
class ApplyReport:
    """What :func:`apply_schedule` wrote."""

    days_recorded: int
    days_cleared: int
    weekends_recorded: int
    weekends_removed: int


def _name(value) -> str | None:
    return None if is_empty(value) else str(value)


def apply_schedule(db_name: str, schedule: pd.DataFrame) -> ApplyReport:
    """Replace recorded history for the schedule's dates with ``schedule``.

    ``schedule`` has a ``DatetimeIndex`` of consecutive days and ``main`` and
    ``backup`` columns. For every day it covers:

    * the per-day assignment history is replaced; a day with nobody on it
      has its record removed rather than kept from an earlier run;
    * each Friday's weekend rotation (Friday main is FSF, Friday backup is
      SFS) is recorded, and a Friday left without a pair has any earlier
      recorded weekend removed, so a replaced schedule leaves nothing behind.

    Rotation history and violation stats are rebuilt once, inside the same
    transaction. Every name must belong to a nurse in the database; otherwise
    ``ValueError`` is raised before anything is written.
    """
    if schedule.empty:
        return ApplyReport(0, 0, 0, 0)

    days = sorted(pd.DatetimeIndex(schedule.index).normalize())
    rows = {
        day: (_name(schedule.at[day, "main"]), _name(schedule.at[day, "backup"])) for day in days
    }

    with sqlite3.connect(db_name) as conn:
        known = {name for (name,) in conn.execute("SELECT name FROM nurses")}
    unknown = sorted({n for pair in rows.values() for n in pair if n and n not in known})
    if unknown:
        raise ValueError(f"Not in the nurse list: {', '.join(unknown)}")

    counts = {"recorded": 0, "cleared": 0, "weekends": 0, "removed": 0}

    def write(conn: sqlite3.Connection) -> None:
        for day, (main, backup) in rows.items():
            date_str = day.strftime("%Y-%m-%d")
            if main is None and backup is None:
                cur = conn.execute("DELETE FROM schedule_history WHERE date = ?", (date_str,))
                counts["cleared"] += cur.rowcount
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO schedule_history (date, main_nurse_id, backup_nurse_id)
                VALUES (?,
                        (SELECT nurse_id FROM nurses WHERE name = ?),
                        (SELECT nurse_id FROM nurses WHERE name = ?))
                """,
                (date_str, main, backup),
            )
            counts["recorded"] += 1

        for day in days:
            if day.weekday() != FRIDAY:
                continue
            date_str = day.strftime("%Y-%m-%d")
            fsf, sfs = rows[day]
            if fsf and sfs and fsf != sfs:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO weekend_assignments
                          (weekend_start, fsf_nurse_id, sfs_nurse_id)
                    VALUES (?,
                            (SELECT nurse_id FROM nurses WHERE name = ?),
                            (SELECT nurse_id FROM nurses WHERE name = ?))
                    """,
                    (date_str, fsf, sfs),
                )
                counts["weekends"] += 1
            else:
                cur = conn.execute(
                    "DELETE FROM weekend_assignments WHERE weekend_start = ?", (date_str,)
                )
                counts["removed"] += cur.rowcount

    WeekendHistory(db_name).weekend_service.run_in_transaction("apply_schedule", write)

    return ApplyReport(
        days_recorded=counts["recorded"],
        days_cleared=counts["cleared"],
        weekends_recorded=counts["weekends"],
        weekends_removed=counts["removed"],
    )


__all__ = ["ApplyReport", "apply_schedule"]
