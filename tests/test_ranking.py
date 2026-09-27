"""Final ranking: repeats, then unfilled slots, then the weighted score.

Covers audit findings 6 and 15 as decided: fewest weekend-pattern repeats
ranks first, then fewest unfilled slots; the weighted score only orders
ties, and rot_viol measures the new repeats a candidate introduces. Exact
ties in the weighted score go to the smaller total spread, then the fewest
same-weekday repeats.
"""

from __future__ import annotations

import pandas as pd
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import SchedulerConfig, WeekendHistory, WeekendPattern
from scheduler.scoring import rank_rows, weekday_repeats

WEIGHTS = SchedulerConfig().scoring_weights


def _row(idx, **metrics):
    base = dict(rotation_rep=0, gaps=0, rot_viol=0, weekend_gap=40, balance=2, long_term=0)
    return {"idx": idx, **base, **metrics}


def _order(rows):
    return list(rank_rows(rows, weights=WEIGHTS)["rank"].sort_values().index)


def test_fewest_repeats_ranks_first_even_over_unfilled_slots():
    rows = [_row(0, rotation_rep=1, gaps=0), _row(1, rotation_rep=0, gaps=3, balance=9)]
    assert _order(rows) == [1, 0]


def test_a_fully_covered_schedule_ranks_above_one_with_a_gap():
    # The reproduction from the audit: a one-point edge elsewhere used to win.
    rows = [_row(0, gaps=1, weekend_gap=40, balance=2), _row(1, gaps=0, weekend_gap=41, balance=3)]
    assert _order(rows) == [1, 0]


def test_a_trivial_difference_does_not_earn_a_metrics_full_weight():
    # One day of weekend spacing against four shifts of balance.
    rows = [_row(0, weekend_gap=41, balance=2), _row(1, weekend_gap=40, balance=6)]
    assert _order(rows) == [0, 1]


def test_the_weights_of_ranked_metrics_are_ignored():
    rows = [_row(0, rotation_rep=0, gaps=1), _row(1, rotation_rep=0, gaps=0, balance=9)]
    loud = dict(WEIGHTS, gaps=0.0, rotation_rep=100.0)
    assert list(rank_rows(rows, weights=loud)["rank"].sort_values().index) == [1, 0]


def test_ties_keep_candidate_order():
    assert _order([_row(3), _row(1), _row(2)]) == [1, 2, 3]


def test_exact_ties_go_to_total_spread_then_weekday_variety():
    rows = [
        _row(0, total_spread=3, weekday_repeats=0),
        _row(1, total_spread=2, weekday_repeats=6),
        _row(2, total_spread=2, weekday_repeats=4),
    ]
    assert _order(rows) == [2, 1, 0]


def test_tie_breakers_never_override_the_weighted_score():
    rows = [
        _row(0, balance=2, total_spread=9, weekday_repeats=9),
        _row(1, balance=6, total_spread=0, weekday_repeats=0),
    ]
    assert _order(rows) == [0, 1]


def test_weekday_repeats_counts_same_weekday_pairs_per_nurse():
    days = pd.date_range("2026-11-02", periods=21, freq="D")  # Mon 2 Nov, three weeks
    df = pd.DataFrame({"main": None, "backup": None, "is_weekend": days.weekday >= 4}, index=days)
    for monday in days[days.weekday == 0]:
        df.loc[monday, ["main", "backup"]] = ["A", "B"]  # A and B: three Mondays each
    df.loc[days[1], "main"] = "A"  # one Tuesday: no pair
    df.loc[days[4], "main"] = "A"  # a Friday: not a weekday row
    df.loc[days[11], "main"] = "C"  # Thursday, but marked as a weekend (a holiday)
    df.loc[days[11], "is_weekend"] = True
    df.loc[days[18], "main"] = "C"
    assert weekday_repeats(df) == 3 + 3


# ── rot_viol: new repeats, weighted by past violations ────────────────────
def _weekends(pairs):
    """A schedule with one Fri–Sun weekend per (fsf, sfs) pair, a week apart."""
    days = pd.date_range("2026-11-06", periods=7 * len(pairs) - 4, freq="D")
    df = pd.DataFrame(index=days, columns=["main", "backup"], data=None)
    for week, (fsf, sfs) in enumerate(pairs):
        friday = days[0] + pd.Timedelta(days=7 * week)
        df.loc[friday] = [fsf, sfs]
        df.loc[friday + pd.Timedelta(days=1)] = [sfs, fsf]
        df.loc[friday + pd.Timedelta(days=2)] = [fsf, sfs]
    return df


def test_rot_viol_weights_each_new_repeat_by_past_violations(tmp_path):
    scheduler = build_scheduler(seed_db(tmp_path), "2026-11-02", "2026-11-29")
    scheduler.last_pattern = {"A": WeekendPattern.FSF, "B": WeekendPattern.FSF}
    prior = {"A": 2, "B": 0}

    repeat_for_a = _weekends([("A", "C")])  # A repeats FSF
    repeat_for_b = _weekends([("B", "C")])  # B repeats FSF
    alternating = _weekends([("C", "A")])  # A switches to SFS: no repeat

    assert scheduler._rotation_violation_score(repeat_for_a, prior) == 3
    assert scheduler._rotation_violation_score(repeat_for_b, prior) == 1
    assert scheduler._rotation_violation_score(alternating, prior) == 0


def test_rot_viol_follows_patterns_within_the_candidate(tmp_path):
    scheduler = build_scheduler(seed_db(tmp_path), "2026-11-02", "2026-11-29")
    scheduler.last_pattern = {}
    # C works FSF twice in a row inside the schedule: one repeat.
    assert scheduler._rotation_violation_score(_weekends([("C", "A"), ("C", "B")]), {}) == 1


def test_past_violations_exclude_the_window_being_replaced(tmp_path):
    db = seed_db(
        tmp_path,
        weekends=[
            ("2026-09-04", "A", "B"),
            ("2026-09-25", "A", "C"),  # A repeats FSF: a violation before the window
            ("2026-11-06", "A", "D"),  # and again inside it: being replaced
        ],
    )
    history = WeekendHistory(db)
    assert history.get_violation_counts()["A"] == 2
    assert history.get_violation_counts_before("2026-11-02")["A"] == 1
    assert build_scheduler(db, "2026-11-02", "2026-11-29")._prior_violation_counts()["A"] == 1


def test_a_violation_count_override_still_applies(tmp_path):
    db = seed_db(tmp_path, weekends=[("2026-09-04", "A", "B"), ("2026-09-25", "A", "C")])
    WeekendHistory(db).set_violation_count("A", 5)
    assert WeekendHistory(db).get_violation_counts_before("2026-11-02")["A"] == 5


def test_the_engine_ranks_candidates_best_first(tmp_path):
    scheduler = build_scheduler(seed_db(tmp_path), "2026-11-02", "2026-11-08")
    variant = scheduler.generate_all_weekend_variants()[0]
    variant.assign_weekdays()
    df = variant.state.schedule
    counts = scheduler.get_nurse_assignment_counts(variant)

    def stats(repeats, gaps):
        return {"rotation_rep": repeats, "gaps": gaps, "balance_main": 1, "balance_backup": 1}

    candidates = [
        (0, stats(1, 0), counts, df),
        (1, stats(0, 2), counts, df),
        (2, stats(0, 0), counts, df),
    ]
    scheduler._score_and_rank_variants(candidates)

    assert [c[0] for c in candidates] == [2, 1, 0]
    assert [c[1]["rank"] for c in candidates] == [1, 2, 3]
    assert candidates[0][1]["weekday_repeats"] == weekday_repeats(df)
    assert "total_spread" in candidates[0][1]
