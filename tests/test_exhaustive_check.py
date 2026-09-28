"""Checking every weekend variant against a run's top options.

``check_all_weekend_variants`` rules variants out with bounds instead of
filling every variant's weekdays. These tests check the bounds against full
evaluations of every variant of small rosters, the ranking logic, and that a
variant that would rank in the top is found.
"""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

import scheduler.optimization.exhaustive as exhaustive
from scheduler.evaluation.worker import _evaluate_variant_core
from scheduler.optimization.exhaustive import _status, check_all_weekend_variants

ROSTER = tuple((name, False, False) for name in "ABCDEF")
WEEKEND_1 = ("2026-11-06", "2026-11-07", "2026-11-08")
WEEKEND_2 = ("2026-11-13", "2026-11-14", "2026-11-15")


def _roster_db(tmp_path, extra_off=(), pre_scheduled=()):
    """Six nurses: A–C can work only the first weekend, D–F only the second."""
    off = [(n, d) for n in "DEF" for d in WEEKEND_1] + [(n, d) for n in "ABC" for d in WEEKEND_2]
    return seed_db(
        tmp_path,
        roster=ROSTER,
        time_off=[*off, *extra_off],
        pre_scheduled=pre_scheduled,
    )


SCENARIOS = {
    "plain": {},
    "time-off": {"extra_off": [("A", "2026-11-03"), ("E", "2026-11-10"), ("B", "2026-11-12")]},
    "pinned": {"pre_scheduled": [("2026-11-04", "C", None)]},
}


def _scheduler(tmp_path, scenario, **config):
    db = _roster_db(tmp_path, **SCENARIOS[scenario])
    config.setdefault("engine", "variants")  # the check is of capped variant runs
    return build_scheduler(db, "2026-11-02", "2026-11-15", **config)


def _actual(scheduler, variant):
    """The ranking measures of *variant* evaluated in full, as a run would."""
    idx, stats, counts, schedule = _evaluate_variant_core(
        (0, variant, scheduler.worker_tuning), with_profiling=False
    )
    return exhaustive._measures(
        scheduler,
        stats,
        counts,
        schedule,
        scheduler._prior_violation_counts(),
        scheduler._historic_overage(),
    )


# ── the bounds, against every variant evaluated in full ────────────────────
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_bounds_never_exceed_the_evaluated_measures(tmp_path, scenario):
    scheduler = _scheduler(tmp_path, scenario, max_weekend_variants=0)
    variants = scheduler.generate_all_weekend_variants()
    assert 10 < len(variants) < 100
    bounds, *_ = exhaustive._measure_and_bound(
        scheduler, variants, scheduler.worker_tuning, 1, lambda *a: None
    )

    for variant, bound in zip(variants, bounds, strict=True):
        actual = _actual(scheduler, variant)
        for measure in ("rot", "gaps", "rot_viol", "weekend_gap"):
            assert bound[measure] == actual[measure], measure  # exact before any solve
        for measure in ("balance", "long_term"):
            assert bound[measure] <= actual[measure], measure


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_exact_minima_are_the_least_any_fill_gives(tmp_path, scenario):
    scheduler = _scheduler(tmp_path, scenario, max_weekend_variants=0)
    variants = scheduler.generate_all_weekend_variants()[:12]
    _bounds, keys, situations, base_main, base_backup, nurses = exhaustive._measure_and_bound(
        scheduler, variants, scheduler.worker_tuning, 1, lambda *a: None
    )
    for vi, variant in enumerate(variants):
        _vi, minima = exhaustive._exact_minima(
            (
                vi,
                [situations[sid][1] for sid in keys[vi]],
                base_main[vi].tolist(),
                base_backup[vi].tolist(),
                [0] * len(nurses),
                ["balance", "long_term"],
            )
        )
        actual = _actual(scheduler, variant)
        # The run minimizes balance right after unfilled slots, so it reaches
        # the least balance; fairness comes later and can be above its least.
        assert minima["balance"] == actual["balance"]
        assert minima["long_term"] <= actual["long_term"]


# ── ranking logic ──────────────────────────────────────────────────────────
WEIGHTED = ["rot_viol", "weekend_gap", "balance", "long_term"]


def _m(**kw):
    base = dict(rot=0, gaps=0, rot_viol=0, weekend_gap=28, balance=2, long_term=10)
    return {**base, **kw}


def test_rank_first_measures_decide_before_the_weighted_ones():
    reference = [_m()]
    assert _status(_m(gaps=1, balance=0, long_term=0), reference, WEIGHTED) == "ruled out"
    assert _status(_m(rot=1, balance=0), reference, WEIGHTED) == "ruled out"
    assert _status(_m(gaps=0, rot=0, balance=9), [_m(gaps=1)], WEIGHTED) == "open"


def test_a_variant_better_on_any_weighted_measure_stays_open():
    reference = [_m()]
    assert _status(_m(long_term=9, balance=5), reference, WEIGHTED) == "open"
    assert _status(_m(balance=3), reference, WEIGHTED) == "ruled out"
    assert _status(_m(), reference, WEIGHTED) == "tie"


def test_it_must_rank_below_every_top_option():
    reference = [_m(), _m(balance=4)]
    assert _status(_m(balance=3), reference, WEIGHTED) == "open"  # beats the second
    assert _status(_m(balance=4), reference, WEIGHTED) == "tie"


def test_measures_without_weight_do_not_count():
    reference = [_m()]
    assert _status(_m(long_term=0, balance=3), reference, ["balance"]) == "ruled out"


# ── end to end ─────────────────────────────────────────────────────────────
def test_a_capped_run_that_found_the_best_is_confirmed(tmp_path):
    scheduler = _scheduler(tmp_path, "plain", max_weekend_variants=1)
    run = scheduler.run_generation(max_workers=1, on_progress=lambda d, n: None)

    report = check_all_weekend_variants(scheduler, run.candidates, top_n=1, workers=1)

    assert report.variants > 10
    assert report.better == []
    assert len(report.statuses) == report.variants
    assert report.ruled_out + report.can_only_tie == report.variants
    assert scheduler.config.max_weekend_variants == 1  # restored


def test_a_better_discarded_variant_is_found(tmp_path):
    scheduler = _scheduler(tmp_path, "time-off", max_weekend_variants=1)
    run = scheduler.run_generation(max_workers=1, on_progress=lambda d, n: None)
    # Pretend the run's option left three more slots empty than it did.
    idx, stats, counts, schedule = run.candidates[0]
    weak = [(idx, {**stats, "gaps": stats["gaps"] + 3}, counts, schedule)]

    report = check_all_weekend_variants(scheduler, weak, top_n=1, workers=1)

    # The better schedules found first become the reference, so the rest
    # need no evaluation.
    assert 0 < report.evaluated < report.variants
    assert len(report.better) == 1
    _idx, found_stats, found_counts, found_schedule = report.better[0]
    assert isinstance(found_schedule, pd.DataFrame)

    # Ground truth: every variant evaluated in full, ranked with the weak option.
    scheduler.config.max_weekend_variants = 0
    everything = [
        _evaluate_variant_core((1000 + i, v, scheduler.worker_tuning), with_profiling=False)
        for i, v in enumerate(scheduler.generate_all_weekend_variants())
    ]
    pool = [(i, dict(st), c, df) for i, st, c, df in [*weak, *everything]]
    scheduler._score_and_rank_variants(pool)
    _i, best_stats, best_counts, best_schedule = pool[0]
    prior, overage = scheduler._prior_violation_counts(), scheduler._historic_overage()

    def measures(st, c, df):
        return exhaustive._measures(scheduler, st, c, df, prior, overage)

    assert measures(found_stats, found_counts, found_schedule) == measures(
        best_stats, best_counts, best_schedule
    )
