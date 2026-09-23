"""consec_violations counts successive worked weekends (audit finding 20)."""

from __future__ import annotations

from scheduling_fixtures import seed_db

from scheduler import WeekendHistory


def _summary(tmp_path, weekends):
    return (
        WeekendHistory(seed_db(tmp_path, weekends=weekends))
        .get_violation_summary()
        .set_index("nurse")
    )


def test_successive_repeat_violations_form_a_streak(tmp_path):
    # A works FSF four times in a row, three weeks apart: three repeats in a row.
    summary = _summary(
        tmp_path,
        [
            ("2026-06-05", "A", "B"),
            ("2026-06-26", "A", "C"),
            ("2026-07-17", "A", "D"),
            ("2026-08-07", "A", "E"),
        ],
    )
    assert summary.at["A", "total_viol"] == 3
    assert summary.at["A", "consec_viol"] == 3


def test_an_alternation_ends_the_streak(tmp_path):
    summary = _summary(
        tmp_path,
        [
            ("2026-06-05", "A", "B"),
            ("2026-06-26", "A", "C"),  # repeat
            ("2026-07-17", "D", "A"),  # alternates: streak over
            ("2026-08-07", "E", "A"),  # repeat again: a new streak of one
        ],
    )
    assert summary.at["A", "total_viol"] == 2
    assert summary.at["A", "consec_viol"] == 1
