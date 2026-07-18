"""Constraint/eligibility helpers for scheduling decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd


@dataclass(frozen=True)
class WeekdayConstraintConfig:
    """Narrow config view used by weekday constraint checks."""

    allow_post_weekend_wednesday_main: bool
    allow_post_weekend_wednesday_backup: bool
    allow_post_weekend_thursday_main: bool
    allow_post_weekend_thursday_backup: bool


def passes_basic_eligibility_checks(
    *,
    nurse: str,
    avail_row: pd.Series,
    other_nurse: Optional[str],
    has_late_shift_conflict: bool,
    diagnostics: Optional[list[str]] = None,
) -> bool:
    """Cheap, deterministic filtering before spacing/weekly-window checks."""
    if nurse == other_nurse:
        if diagnostics is not None:
            diagnostics.append("other_role_conflict")
        return False

    try:
        av_val = avail_row.get(nurse, False)
        if pd.isna(av_val) or not bool(av_val):
            if diagnostics is not None:
                diagnostics.append("unavailable")
            return False
    except Exception:
        if diagnostics is not None:
            diagnostics.append("unavailable")
        return False

    if has_late_shift_conflict:
        if diagnostics is not None:
            diagnostics.append("late_shift_conflict")
        return False

    return True


def validate_post_weekend_assignment(
    *,
    weekday: int,
    role: str,
    config: WeekdayConstraintConfig,
) -> bool:
    """Return whether a weekday role is allowed in post-weekend window."""
    if weekday == 2:
        if role == "main" and not config.allow_post_weekend_wednesday_main:
            return False
        if role == "backup" and not config.allow_post_weekend_wednesday_backup:
            return False
        return True
    if weekday == 3:
        if role == "main" and not config.allow_post_weekend_thursday_main:
            return False
        if role == "backup" and not config.allow_post_weekend_thursday_backup:
            return False
        return True
    return False


def validate_weekday_relative_to_weekend(
    *,
    nurse: str,
    date: pd.Timestamp,
    role: str,
    config: WeekdayConstraintConfig,
    is_in_pre_weekend_window: Callable[[str, pd.Timestamp], bool],
    is_in_post_weekend_window: Callable[[str, pd.Timestamp], bool],
) -> bool:
    """Shared weekday-weekend guard used by normal weekday assignment."""
    weekday = date.weekday()
    if is_in_pre_weekend_window(nurse, date):
        # Only Monday is allowed leading into a worked weekend.
        if weekday != 0:
            return False
        # A Monday that also falls in the post-weekend window still owes the
        # 2-day post-weekend gap, so fall through to the post-weekend check
        # rather than returning early (this only arises with back-to-back
        # weekends for the same nurse).
    if is_in_post_weekend_window(nurse, date):
        return validate_post_weekend_assignment(weekday=weekday, role=role, config=config)
    return True


def validate_weekday_relative_to_weekend_gap(
    *,
    nurse: str,
    date: pd.Timestamp,
    role: str,
    config: WeekdayConstraintConfig,
    is_in_pre_weekend_window: Callable[[str, pd.Timestamp], bool],
    is_in_post_weekend_window: Callable[[str, pd.Timestamp], bool],
) -> bool:
    """Gap-fill variant of weekday-weekend guard (allows Tue in pre-window)."""
    weekday = date.weekday()
    if is_in_pre_weekend_window(nurse, date) and weekday not in (0, 1):
        return False
    if is_in_post_weekend_window(nurse, date):
        return validate_post_weekend_assignment(weekday=weekday, role=role, config=config)
    return True
