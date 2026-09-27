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


def test_a_prn_nurse_pinned_into_a_weekend_is_refused(tmp_path):
    roster = (*((n, False, False) for n in "ABCDEF"), ("P", True, False))
    db = seed_db(
        tmp_path,
        roster=roster,
        pre_scheduled=[("2026-11-06", "P", None), ("2026-11-07", None, "P")],
    )
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15")
    assert "P" in mm.unsupported(scheduler)
    assert mm.solve_month_status(scheduler)[0] == "unsupported"


# ── the one-day gap ────────────────────────────────────────────────────────
def _short_staffed(tmp_path, **config):
    """Six nurses, three of them off every weekday of the first week.

    With 2-day spacing three nurses cannot cover four days; the one-day gap
    (Mon–Wed, Tue–Thu) can.
    """
    off = _only(["ABC", "DEF"], "ABCDEF")
    off += [(n, f"2026-11-0{d}") for n in "DEF" for d in (2, 3, 4, 5)]
    db = seed_db(tmp_path, roster=ROSTER6, time_off=off)
    return build_scheduler(db, "2026-11-02", "2026-11-15", **config)


def test_the_one_day_gap_fills_what_spacing_alone_cannot(tmp_path):
    (strict,) = mm.solve_month(_short_staffed(tmp_path))
    relaxed_scheduler = _short_staffed(tmp_path, allow_one_day_weekday_gap=True)
    (relaxed,) = mm.solve_month(relaxed_scheduler)

    assert relaxed.values["gaps"] < strict.values["gaps"]
    assert relaxed.values["relaxed"] > 0
    _variant, replayed, _counts = mm.replay(relaxed_scheduler, relaxed)
    assert replayed == relaxed.values
    # Without the setting the same month breaks the spacing rule.
    with pytest.raises(ValueError):
        mm.replay(_short_staffed(tmp_path), relaxed)


def test_the_one_day_gap_is_only_a_fallback(tmp_path):
    """Eight nurses fill every slot with ordinary spacing: no gap is used."""
    plain = _scheduler(tmp_path, "history")
    relaxed_scheduler = _scheduler(tmp_path, "history", allow_one_day_weekday_gap=True)
    (without,) = mm.solve_month(plain)
    (with_gap,) = mm.solve_month(relaxed_scheduler)
    assert without.values["gaps"] == with_gap.values["gaps"] == 0
    assert with_gap.values["relaxed"] == 0


def test_role_limited_one_day_gaps_keep_to_their_roles(tmp_path):
    """Only a Main and a Backup may be a day apart: never two Backups."""
    scheduler = _short_staffed(tmp_path, allow_midweek_pair_mixed=True)
    (solution,) = mm.solve_month(scheduler)
    _variant, replayed, _counts = mm.replay(scheduler, solution)  # re-checks the roles
    assert replayed == solution.values
    days = {}
    for (day, role), nurse in solution.weekdays.items():
        days.setdefault(nurse, []).append((day, role))
    pairs = [
        (a, b)
        for shifts in days.values()
        for a in shifts
        for b in shifts
        if a[0] < b[0] and (b[0] - a[0]).days == 2
    ]
    assert pairs and all({a[1], b[1]} == {"main", "backup"} for a, b in pairs)


def test_one_day_gap_coverage_matches_the_exact_solve(tmp_path):
    scheduler = _short_staffed(tmp_path, allow_one_day_weekday_gap=True)
    variants, _ = _generated_arrangements(scheduler)
    for variant in variants[:4]:
        schedule = variant.state.schedule
        fixed = {f: tuple(schedule.loc[f, ["main", "backup"]]) for f in scheduler._get_weekends()}
        (solution,) = mm.solve_month(scheduler, fixed_weekends=fixed, order=mm.WEEKDAY_ORDER)
        exact = variant.clone()
        exact.compute_unfillable_slots()
        outcome = solve_weekdays_exactly(exact)
        assert solution.values["gaps"] == outcome.gaps + len(exact.unfillable_slots)
        _v, replayed, _counts = mm.replay(scheduler, solution)
        assert {k: replayed[k] for k in solution.values} == solution.values


# ── relaxed rotation ───────────────────────────────────────────────────────
def _no_alternation(tmp_path):
    """Only nurses whose last weekend was FSF can work the first weekend,
    and only ones whose last was SFS the second: strict alternation fails."""
    history = [("2026-09-25", "A", "D"), ("2026-10-02", "B", "E"), ("2026-10-09", "C", "F")]
    db = seed_db(
        tmp_path, roster=ROSTER6, weekends=history, time_off=_only(["ABC", "DEF"], "ABCDEF")
    )
    return build_scheduler(db, "2026-11-02", "2026-11-15")


def test_strict_alternation_can_be_proven_impossible(tmp_path):
    assert mm.solve_month_status(_no_alternation(tmp_path))[0] == "infeasible"


def test_relaxed_rotation_repeats_as_little_as_possible(tmp_path):
    scheduler = _no_alternation(tmp_path)
    (solution,) = mm.solve_month(scheduler, allow_rotation_violations=True)
    _v, replayed, _counts = mm.replay(scheduler, solution, allow_rotation_violations=True)
    assert replayed == solution.values
    assert solution.values["rotation"] == 2  # one repeat on each weekend

    generated = _no_alternation(tmp_path).generate_weekend_candidates(
        allow_rotation_violations=True
    )
    assert min(len(v.rotation_violations) for v in generated.variants) >= 2


def test_relaxed_rotation_allows_every_arrangement_the_generator_does(tmp_path):
    built = mm._Model(_no_alternation(tmp_path), None, [], allow_rotation_violations=True)
    solver = built.cp.CpSolver()
    solver.parameters.num_workers = 8
    model_set = set()
    while solver.Solve(built.m) in (built.cp.OPTIMAL, built.cp.FEASIBLE):
        arrangement = tuple(
            (
                f,
                next(n for n in built.nurses if solver.BooleanValue(built.fsf[f, n])),
                next(n for n in built.nurses if solver.BooleanValue(built.sfs[f, n])),
            )
            for f in built.fridays
        )
        model_set.add(arrangement)
        built.m.Add(sum(built.fsf[f, a] + built.sfs[f, b] for f, a, b in arrangement) <= 3)
    scheduler = _no_alternation(tmp_path)
    fridays = scheduler._get_weekends()
    generated = {
        tuple(
            (f, v.state.schedule.at[f, "main"], v.state.schedule.at[f, "backup"]) for f in fridays
        )
        for v in scheduler.generate_weekend_candidates(allow_rotation_violations=True).variants
    }
    assert generated and generated <= model_set


def test_only_the_nurses_allowed_to_repeat_do(tmp_path):
    scheduler = _no_alternation(tmp_path)
    scheduler.nurses_allowed_rotation_violation = {"B", "E"}
    (solution,) = mm.solve_month(scheduler, allow_rotation_violations=True)
    variant, _replayed, _counts = mm.replay(scheduler, solution, allow_rotation_violations=True)
    assert {nurse for _f, nurse, _p in variant.rotation_violations} <= {"B", "E"}


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
