import pandas as pd

from scheduler.constraints import (
    WeekdayConstraintConfig,
    passes_basic_eligibility_checks,
    validate_weekday_relative_to_weekend,
    validate_weekday_relative_to_weekend_gap,
)


def _cfg() -> WeekdayConstraintConfig:
    return WeekdayConstraintConfig(
        allow_post_weekend_wednesday_main=False,
        allow_post_weekend_wednesday_backup=True,
        allow_post_weekend_thursday_main=True,
        allow_post_weekend_thursday_backup=True,
    )


def test_passes_basic_eligibility_rejects_nan_unavailability():
    avail = pd.Series({"Alice": float("nan"), "Bob": True})
    reasons: list[str] = []
    ok = passes_basic_eligibility_checks(
        nurse="Alice",
        avail_row=avail,
        other_nurse=None,
        has_late_shift_conflict=False,
        diagnostics=reasons,
    )
    assert ok is False
    assert "unavailable" in reasons


def test_validate_weekday_relative_to_weekend_honors_pre_post_windows():
    dt = pd.Timestamp("2026-01-07")  # Wednesday
    ok = validate_weekday_relative_to_weekend(
        nurse="Alice",
        date=dt,
        role="main",
        config=_cfg(),
        is_in_pre_weekend_window=lambda *_: False,
        is_in_post_weekend_window=lambda *_: True,
    )
    assert ok is False


def test_validate_weekday_relative_to_weekend_gap_allows_tuesday_pre_window():
    dt = pd.Timestamp("2026-01-06")  # Tuesday
    ok = validate_weekday_relative_to_weekend_gap(
        nurse="Alice",
        date=dt,
        role="backup",
        config=_cfg(),
        is_in_pre_weekend_window=lambda *_: True,
        is_in_post_weekend_window=lambda *_: False,
    )
    assert ok is True
