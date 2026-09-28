"""Shape schedules, calendars and counts for the web pages.

The pages are laid out for a phone first: a schedule is a list of days
grouped into Monday-to-Sunday weeks, so each Friday-to-Sunday call weekend
stays together, rather than a seven-column calendar that would be unreadable
at phone width. The printable month grid is the PDF export's job.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from scheduler.runtime import is_empty

# (stats key, label, help) for the numbers shown with each generated option,
# worded as in the desktop app's review dialog.
METRICS = [
    (
        "rotation_rep",
        "Rotation repeats",
        "Weekends where a nurse repeats last time's FSF/SFS pattern. Ranked by this first. "
        "Ideally 0.",
    ),
    ("gaps", "Unfilled slots", "Main/Backup slots nobody could fill. Ideally 0."),
    (
        "unfillable",
        "Impossible slots",
        "Unfilled slots no nurse could legally take, because of time off, spacing or the "
        "weekend rules.",
    ),
    ("balance_main", "Main spread", "Most minus fewest Main shifts any nurse gets."),
    ("balance_backup", "Backup spread", "Most minus fewest Backup shifts any nurse gets."),
    ("weighted_score", "Score", "Orders options that tie on the above. Lower is better."),
]


@dataclass(frozen=True)
class Day:
    day: date
    main: str | None
    backup: str | None

    @property
    def weekend(self) -> bool:
        return self.day.weekday() >= 4  # Friday to Sunday

    @property
    def filled(self) -> bool:
        return bool(self.main or self.backup)


@dataclass(frozen=True)
class Week:
    monday: date
    days: list[Day]


def _name(value) -> str | None:
    return None if is_empty(value) else str(value)


def days_from_frame(schedule: pd.DataFrame) -> list[Day]:
    """Every day of a generated schedule, in order."""
    out = []
    for stamp, row in schedule.sort_index().iterrows():
        out.append(Day(pd.Timestamp(stamp).date(), _name(row["main"]), _name(row["backup"])))
    return out


def days_from_history(records, start: date, end: date) -> list[Day]:
    """Every day in ``[start, end]``, from ``(date_str, main, backup)`` records."""
    by_day = {date.fromisoformat(d[:10]): (m, b) for d, m, b in records}
    out = []
    day = start
    while day <= end:
        main, backup = by_day.get(day, (None, None))
        out.append(Day(day, _name(main), _name(backup)))
        day += timedelta(days=1)
    return out


def weeks(days: list[Day]) -> list[Week]:
    """``days`` grouped into Monday-to-Sunday weeks."""
    out: list[Week] = []
    for d in days:
        monday = d.day - timedelta(days=d.day.weekday())
        if not out or out[-1].monday != monday:
            out.append(Week(monday, []))
        out[-1].days.append(d)
    return out


def frame_from_days(days: list[Day]) -> pd.DataFrame:
    """A schedule frame (``main``/``backup`` by day) for the PDF exporter."""
    index = pd.DatetimeIndex([pd.Timestamp(d.day) for d in days])
    return pd.DataFrame(
        {"main": [d.main or "" for d in days], "backup": [d.backup or "" for d in days]},
        index=index,
    )


def counts(days: list[Day]) -> list[tuple[str, int, int, int]]:
    """``(nurse, main, backup, total)`` for everyone scheduled, by name."""
    tally: dict[str, list[int]] = {}
    for d in days:
        if d.main:
            tally.setdefault(d.main, [0, 0])[0] += 1
        if d.backup:
            tally.setdefault(d.backup, [0, 0])[1] += 1
    return [(n, m, b, m + b) for n, (m, b) in sorted(tally.items(), key=lambda i: i[0].casefold())]


def month_grid(year: int, month: int) -> list[list[date | None]]:
    """Sunday-first weeks of the month, ``None`` outside it (as the PDF prints)."""
    cal = calendar.Calendar(firstweekday=6)
    return [
        [date(year, month, d) if d else None for d in week]
        for week in cal.monthdayscalendar(year, month)
    ]


def parse_month(value: str | None, default: date) -> tuple[int, int]:
    """``"YYYY-MM"`` as ``(year, month)``, or ``default``'s month if unreadable."""
    try:
        year, month = (int(part) for part in (value or "").split("-"))
        date(year, month, 1)
        return year, month
    except ValueError:
        return default.year, default.month


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def shift_month(year: int, month: int, by: int) -> str:
    """``"YYYY-MM"`` for ``by`` months after (or before) the given one."""
    index = year * 12 + (month - 1) + by
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def span(start: date, end: date) -> str:
    """``"Mon Oct 05 – Sun Nov 01, 2026"``."""
    first = start.strftime("%a %b %d") if start.year == end.year else start.strftime("%a %b %d, %Y")
    return f"{first} – {end.strftime('%a %b %d, %Y')}"


def elapsed(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def metric(key: str, value) -> str:
    if value is None:
        return "—"
    if key == "weighted_score":
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


__all__ = [
    "METRICS",
    "Day",
    "Week",
    "counts",
    "days_from_frame",
    "days_from_history",
    "elapsed",
    "frame_from_days",
    "metric",
    "month_bounds",
    "month_grid",
    "parse_month",
    "shift_month",
    "span",
    "weeks",
]
