"""Recent assignment history reaches the variants (audit findings 9 and 19)."""

from __future__ import annotations

import pickle

from scheduling_fixtures import build_scheduler, seed_db

from scheduler.domain import ScheduleQuality

# A worked four recent shifts before the Nov 2 window; nobody else did.
RECENT = [
    ("2026-10-20", "A", "B"),
    ("2026-10-22", "A", "C"),
    ("2026-10-26", "A", None),
    ("2026-10-28", "A", None),
]


def test_variants_carry_the_recent_assignment_history(tmp_path):
    db = seed_db(tmp_path, history=RECENT)
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-08")

    variant = scheduler.generate_all_weekend_variants()[0]

    assert variant.hist_main.get("A") == 4
    assert variant.hist_backup.get("B") == 1
    assert variant.clone().hist_main == variant.hist_main


def test_the_history_tie_break_prefers_the_nurse_who_worked_less_recently(tmp_path):
    db = seed_db(tmp_path, history=RECENT)
    variant = build_scheduler(db, "2026-11-02", "2026-11-08").generate_all_weekend_variants()[0]
    # Two nurses equal on every earlier key: role, weekday and total counts.
    for nurse in ("A", "D"):
        variant.state.main_assignment_counts[nurse] = 0
        variant.state.backup_assignment_counts[nurse] = 0
    variant._invalidate_weekday_cache()

    assert variant._select_best_candidate(["A", "D"], "main") == "D"


def test_local_search_scores_long_term_fairness_without_the_scheduler(tmp_path):
    db = seed_db(tmp_path, history=RECENT)
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-08")
    variant = scheduler.generate_all_weekend_variants()[0]
    variant.assign_weekdays()

    # Workers get the variant, not the scheduler, so the overage travels with it.
    shipped = pickle.loads(pickle.dumps(variant.clone()))

    assert shipped.historic_overage == scheduler._historic_overage()
    assert shipped.historic_overage["A"] > 0
    assert ScheduleQuality.from_variant(shipped).history_penalty == float(
        scheduler._long_term_score(
            scheduler.get_nurse_assignment_counts(variant), shipped.historic_overage
        )
    )
    assert ScheduleQuality.from_variant(shipped).history_penalty > 0


def test_history_is_read_relative_to_the_schedule_not_today(tmp_path):
    # A schedule for a period long past (more than history_duration_months
    # before today) still sees the 30 days of history before it (finding 23).
    db = seed_db(tmp_path, history=[("2024-01-20", "A", "B"), ("2024-01-22", "A", "C")])
    scheduler = build_scheduler(db, "2024-02-05", "2024-02-11")

    assert scheduler._historical_main["A"] == 2
    assert scheduler._historical_backup["B"] == 1
