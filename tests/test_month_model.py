"""The whole-month CP-SAT model against the scheduler's own rules.

The model encodes the rules a second time, as constraints, so these tests
compare it with the code it mirrors: the weekend arrangements it allows are
exactly the ones weekend generation produces, its weekday fills match the
exact weekday solve on fixed weekends, and every solution replays through
the scheduler's checks with the same measures.
"""

from __future__ import annotations

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler.evaluation.worker import _evaluate_variant_core
from scheduler.optimization import month_model as mm
from scheduler.optimization.exact_weekdays import solve_weekdays_exactly

ROSTER6 = tuple((name, False, False) for name in "ABCDEF")
FRIDAYS = ["2026-11-06", "2026-11-13", "2026-11-20", "2026-11-27", "2026-12-04"]


def _weekend(friday):
    f = pd.Timestamp(friday)
    return [str((f + pd.Timedelta(days=i)).date()) for i in range(3)]


def _only(allowed_by_weekend, roster="ABCDEFGH"):
    """Time off leaving only the given nurses available on each weekend."""
    return [
        (n, d)
        for friday, allowed in zip(FRIDAYS, allowed_by_weekend, strict=False)
        for n in roster
        if n not in allowed
        for d in _weekend(friday)
    ]


SCENARIOS = {
    "six": (
        {"roster": ROSTER6, "time_off": _only(["ABC", "DEF"], "ABCDEF")},
        "2026-11-02",
        "2026-11-15",
        {},
    ),
    "history": (
        {
            "weekends": [("2026-10-30", "A", "B"), ("2026-10-16", "C", "D")],
            "time_off": [("E", "2026-11-07")],
        },
        "2026-11-02",
        "2026-11-15",
        {},
    ),
    "five-weekends": (
        {
            "time_off": _only(["ABC", "DEF", "GHA", "BCD", "ABE"]),
            "weekends": [("2026-10-23", "A", "D"), ("2026-10-30", "B", "E")],
        },
        "2026-11-02",
        "2026-12-06",
        {},
    ),
    "post-weekend-settings": (
        {"time_off": _only(["ABC", "DEF", "GHA", "BCD", "ABE"])},
        "2026-11-02",
        "2026-12-06",
        {"allow_post_weekend_wednesday_main": True, "allow_post_weekend_thursday_backup": False},
    ),
    "late-shift": (
        {
            "roster": (
                ("A", False, True),
                ("B", False, True),
                ("C", False, False),
                ("D", False, False),
                ("E", False, False),
                ("F", False, False),
            ),
            "time_off": _only(["ABCD", "ABEF"], "ABCDEF"),
        },
        "2026-11-02",
        "2026-11-15",
        {},
    ),
    "cut-start": (
        {
            "weekends": [("2026-10-30", "A", "B")],
            "time_off": [("C", "2026-11-03"), *_only(["CDEF", "ABGH"])],
        },
        "2026-10-31",
        "2026-11-15",
        {},
    ),
}


def _scheduler(tmp_path, name, **extra):
    db_kwargs, start, end, config = SCENARIOS[name]
    return build_scheduler(seed_db(tmp_path, **db_kwargs), start, end, **config, **extra)


def _model_arrangements(scheduler, limit=2000):
    """Every weekend arrangement the model allows, by excluding each in turn."""
    built = mm._Model(scheduler, None, [])
    solver = built.cp.CpSolver()
    solver.parameters.num_workers = 8
    found = set()
    while len(found) < limit:
        if solver.Solve(built.m) not in (built.cp.OPTIMAL, built.cp.FEASIBLE):
            break
        arrangement = tuple(
            (
                f,
                next(n for n in built.nurses if solver.BooleanValue(built.fsf[f, n])),
                next(n for n in built.nurses if solver.BooleanValue(built.sfs[f, n])),
            )
            for f in built.fridays
        )
        found.add(arrangement)
        chosen = [built.fsf[f, a] + built.sfs[f, b] for f, a, b in arrangement]
        built.m.Add(sum(chosen) <= 2 * len(arrangement) - 1)
    return found


def _generated_arrangements(scheduler):
    scheduler.config.max_weekend_variants = 0
    variants = scheduler.generate_all_weekend_variants()
    fridays = scheduler._get_weekends()
    return variants, {
        tuple(
            (f, v.state.schedule.at[f, "main"], v.state.schedule.at[f, "backup"]) for f in fridays
        )
        for v in variants
    }


# ── weekends: exactly the generator's arrangements ─────────────────────────
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_model_allows_exactly_the_generated_weekends(tmp_path, name):
    model = _model_arrangements(_scheduler(tmp_path, name))
    _variants, generated = _generated_arrangements(_scheduler(tmp_path, name))

    assert 1 < len(generated) < 2000
    assert model == generated


# ── weekdays: the exact solve's result on the same weekends ────────────────
@pytest.mark.parametrize("name", ["six", "history", "cut-start"])
def test_weekdays_match_the_exact_solve_on_fixed_weekends(tmp_path, name):
    scheduler = _scheduler(tmp_path, name)
    variants, _ = _generated_arrangements(scheduler)
    compared = 0
    for variant in variants[:: max(1, len(variants) // 6)][:6]:
        schedule = variant.state.schedule
        fixed = {f: tuple(schedule.loc[f, ["main", "backup"]]) for f in scheduler._get_weekends()}
        (solution,) = mm.solve_month(scheduler, fixed_weekends=fixed, order=mm.WEEKDAY_ORDER)
        exact = variant.clone()
        exact.compute_unfillable_slots()
        outcome = solve_weekdays_exactly(exact)
        _v, replayed, _counts = mm.replay(scheduler, solution)
        assert {k: replayed[k] for k in solution.values} == solution.values
        if not outcome.optimal:
            continue  # the exact solve ran out of time; nothing to compare
        backup, main, total = exact.spread_components()
        assert solution.values["gaps"] == outcome.gaps + len(exact.unfillable_slots)
        assert solution.values["balance"] == backup + main
        assert solution.values["larger"] == max(backup, main)
        assert solution.values["total"] == total
        compared += 1
    assert compared > 0


# ── whole months ───────────────────────────────────────────────────────────
def test_every_solution_replays_with_the_same_measures(tmp_path):
    scheduler = build_scheduler(
        seed_db(tmp_path, time_off=[("A", "2026-11-03"), ("C", "2026-11-18")]),
        "2026-11-02",
        "2026-11-29",
    )
    solutions = mm.solve_month(scheduler, top_n=3)

    assert len(solutions) == 3
    assert len({tuple(s.weekends.items()) for s in solutions}) == 3  # different weekends
    keys = [tuple(s.values[k] for k in mm.MONTH_ORDER) for s in solutions]
    assert keys == sorted(keys)  # best first
    for solution in solutions:
        assert solution.optimal
        _variant, replayed, _counts = mm.replay(scheduler, solution)
        assert replayed == solution.values


def test_the_best_month_is_at_least_as_good_as_every_variant(tmp_path):
    """On a roster small enough to evaluate every weekend variant."""
    scheduler = _scheduler(tmp_path, "six")
    (best,) = mm.solve_month(scheduler)
    variants, _ = _generated_arrangements(_scheduler(tmp_path, "six"))
    for index, variant in enumerate(variants):
        _i, stats, counts, schedule = _evaluate_variant_core(
            (index, variant, scheduler.worker_tuning), with_profiling=False
        )
        key = (
            stats["rotation_rep"],
            stats["gaps"],
            scheduler._weekend_gap_penalty(schedule),
            stats["balance_main"] + stats["balance_backup"],
        )
        month = tuple(best.values[k] for k in ("rotation", "gaps", "weekend_gap", "balance"))
        assert month <= key


def test_longer_spacing_the_exact_solve_cannot_split_by_week(tmp_path):
    scheduler = _scheduler(tmp_path, "five-weekends", min_days_between_assignments=4)
    (solution,) = mm.solve_month(scheduler)
    _variant, replayed, _counts = mm.replay(scheduler, solution)
    assert replayed == solution.values


def test_unsupported_rules_are_refused(tmp_path):
    scheduler = _scheduler(tmp_path, "six", allow_one_day_weekday_gap=True)
    assert "one-day" in mm.unsupported(scheduler)
    assert mm.solve_month(scheduler) == []


def test_replay_rejects_a_month_that_breaks_a_rule(tmp_path):
    scheduler = _scheduler(tmp_path, "six")
    (solution,) = mm.solve_month(scheduler)
    # One nurse in both roles on the same day.
    day = next(
        d
        for (d, role) in sorted(solution.weekdays)
        if role == "main" and (d, "backup") in solution.weekdays
    )
    solution.weekdays[day, "backup"] = solution.weekdays[day, "main"]
    with pytest.raises(ValueError):
        mm.replay(scheduler, solution)


def test_the_same_month_every_run(tmp_path):
    scheduler = _scheduler(tmp_path, "history")
    first, second = mm.solve_month(scheduler, top_n=2), mm.solve_month(scheduler, top_n=2)
    assert [(s.weekends, s.weekdays) for s in first] == [(s.weekends, s.weekdays) for s in second]
