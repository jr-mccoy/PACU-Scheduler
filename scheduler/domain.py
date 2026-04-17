"""This module owns scheduler domain configuration and enums; legacy import remains temporary."""

from enum import Enum

from .legacy_core import ScheduleQuality, ScheduleState, ScheduleVariant


class WeekendPattern(str, Enum):
    """Weekend shift patterns."""

    FSF = "FSF"
    SFS = "SFS"


class SchedulerConfig:
    """Configuration object for scheduling rules and score weighting."""

    def __init__(
        self,
        weekend_gap_days: int = 14,
        main_score_factor: int = 10,
        backup_score_factor: int = 10,
        availability_penalty: int = 10,
        min_days_between_assignments: int = 2,
        allow_post_weekend_wednesday_main: bool = False,
        allow_post_weekend_wednesday_backup: bool = True,
        allow_post_weekend_thursday_main: bool = True,
        allow_post_weekend_thursday_backup: bool = True,
        *,
        scoring_weights: dict[str, float] | None = None,
        allow_one_day_weekday_gap: bool = False,
        **extra,
    ):
        self.allow_one_day_weekday_gap = bool(allow_one_day_weekday_gap)
        self.weekend_gap_days = weekend_gap_days
        self.main_score_factor = main_score_factor
        self.backup_score_factor = backup_score_factor
        self.availability_penalty = availability_penalty
        self.min_days_between_assignments = min_days_between_assignments
        self.allow_post_weekend_wednesday_main = allow_post_weekend_wednesday_main
        self.allow_post_weekend_wednesday_backup = allow_post_weekend_wednesday_backup
        self.allow_post_weekend_thursday_main = allow_post_weekend_thursday_main
        self.allow_post_weekend_thursday_backup = allow_post_weekend_thursday_backup

        default_weights = {
            "rotation_rep": 0.30,
            "gaps": 0.20,
            "rot_viol": 0.15,
            "weekend_gap": 0.15,
            "balance": 0.10,
            "long_term": 0.10,
        }

        merged = default_weights.copy()
        if scoring_weights is not None:
            for key, value in scoring_weights.items():
                if key in merged:
                    merged[key] = max(0.0, float(value))

        total = sum(merged.values()) or 1.0
        self.scoring_weights = {key: value / total for key, value in merged.items()}

        for key, value in extra.items():
            setattr(self, key, value)


__all__ = [
    "WeekendPattern",
    "SchedulerConfig",
    "ScheduleState",
    "ScheduleVariant",
    "ScheduleQuality",
]
