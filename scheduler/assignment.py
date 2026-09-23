"""Assignment mutation helpers with deterministic apply/revert semantics."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class AssignmentMutation:
    date: pd.Timestamp
    role: str
    previous_nurse: str | None
    next_nurse: str | None


def apply_assignment(
    *,
    schedule_df: pd.DataFrame,
    main_counts: pd.Series,
    backup_counts: pd.Series,
    date: pd.Timestamp,
    role: str,
    nurse: str,
) -> AssignmentMutation:
    """Apply a single assignment and update the counters."""
    previous = schedule_df.at[date, role]
    schedule_df.at[date, role] = nurse

    if role == "main":
        main_counts[nurse] += 1
    else:
        backup_counts[nurse] += 1

    return AssignmentMutation(
        date=date,
        role=role,
        previous_nurse=previous,
        next_nurse=nurse,
    )


def revert_assignment(
    *,
    schedule_df: pd.DataFrame,
    main_counts: pd.Series,
    backup_counts: pd.Series,
    mutation: AssignmentMutation,
) -> None:
    """Revert assignment and guard against negative counters."""
    date, role, nurse = mutation.date, mutation.role, mutation.next_nurse
    schedule_df.at[date, role] = mutation.previous_nurse

    if nurse is not None:
        if role == "main":
            main_counts[nurse] = max(0, int(main_counts[nurse]) - 1)
        else:
            backup_counts[nurse] = max(0, int(backup_counts[nurse]) - 1)
