"""Check every weekend variant, not only the capped beam, against a run's top options.

A run keeps at most ``max_weekend_variants`` weekend variants, chosen by
weekend-only measures, and fills the weekdays of each. This check answers
whether any of the variants the cap discarded would have ranked among the
run's top options, without filling the weekdays of every one of them:

1. **Every variant.** Weekend generation runs uncapped. Each variant's
   weekend-only measures (rotation repeats, rotation score, weekend spacing)
   are exact before any weekday is filled.
2. **Each week situation once.** A week's legal fills depend only on the
   cells near it and on who works the weekends either side of it (see
   ``exact_weekdays.week_options``), so variants share weeks: the March
   roster's 63,744 variants have 3,806 distinct week situations. Each is
   listed once, as the count patterns its fills give.
3. **Bounds.** From those patterns every variant gets its exact number of
   unfilled slots and lower bounds on its balance (main + backup spread) and
   long-term fairness penalty. A variant is ruled out when, against each of
   the top options, it ranks lower on the rank-first measures (rotation
   repeats, then unfilled slots) or is no better on any weighted measure.
   That holds whatever the weights and however the ranking normalizes them.
4. **Exact minima.** For each variant still open, CP-SAT finds the least
   balance and the least fairness penalty any legal fill can give (one
   small solve each), and step 3 is repeated with those.
5. **Full evaluation.** Only the variants still open are evaluated as a run
   would evaluate them, then ranked together with the run's candidates.

The per-variant objective is the run's own (fewest unfilled slots, then
balance, ...), so "better" means better as a run would have found and
ranked it.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from ..domain import spread_lower_bound_for_total
from . import exact_weekdays as exact

logger = logging.getLogger(__name__)

RANK_FIRST = ("rot", "gaps")
WEIGHTED = ("rot_viol", "weekend_gap", "balance", "long_term")


@dataclass
class VariantCheckReport:
    """What :func:`check_all_weekend_variants` found."""

    variants: int  # weekend variants without the cap
    situations: int  # distinct week situations among them
    ruled_out: int  # provably rank below each of the top options
    can_only_tie: int  # at best equal a top option on every ranking measure
    solved: int  # needed the exact minima of step 4
    evaluated: int  # needed a full evaluation
    unchecked: int  # a week had too many fills to list; not decided
    better: list = field(default_factory=list)  # (idx, stats, counts, schedule) ranking in the top
    reference: list = field(default_factory=list)  # measures of the top options
    seconds: float = 0.0
    # Per variant, in uncapped generation order: "ruled out", "tie", "open"
    # (evaluated in full) or "unchecked".
    statuses: list = field(default_factory=list)


def check_all_weekend_variants(
    scheduler,
    candidates: list,
    *,
    top_n: int = 5,
    allow_rotation_violations: bool = False,
    workers: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> VariantCheckReport:
    """Check every weekend variant against the top ``top_n`` of *candidates*.

    *candidates* is a run's ranked candidate list (``run.candidates``), all of
    it: the final ranking normalizes over the whole pool, so new candidates
    are ranked against the same pool.
    """
    began = time.perf_counter()
    workers = workers or os.cpu_count() or 1
    report = progress or (lambda stage, done, total: None)
    tuning = scheduler.worker_tuning
    weights = scheduler.config.scoring_weights
    weighted = [m for m in WEIGHTED if weights.get(m, 0) > 0]
    prior, overage = scheduler._prior_violation_counts(), scheduler._historic_overage()

    reference = [
        _measures(scheduler, stats, counts, schedule, prior, overage)
        for _idx, stats, counts, schedule in candidates[:top_n]
    ]

    # 1. Every variant, uncapped.
    report("generate", 0, 1)
    saved = scheduler.config.max_weekend_variants
    scheduler.config.max_weekend_variants = 0
    try:
        variants = scheduler.generate_weekend_candidates(
            allow_rotation_violations=allow_rotation_violations
        ).variants
    finally:
        scheduler.config.max_weekend_variants = saved
    if not variants:
        return VariantCheckReport(0, 0, 0, 0, 0, 0, 0, reference=reference)
    reason = exact.unavailable_reason(variants[0])
    if reason is not None:
        raise ValueError(f"the check needs the exact weekday solve: {reason}")

    bounds, keys, situations, base_main, base_backup, nurses = _measure_and_bound(
        scheduler, variants, tuning, workers, report
    )
    status = [_status(b, reference, weighted) for b in bounds]

    # 4. Exact minima for the variants still open.
    open_ids = [vi for vi, s in enumerate(status) if s == "open"]
    unchecked = [vi for vi in open_ids if bounds[vi] is None]
    to_solve = [vi for vi in open_ids if bounds[vi] is not None]
    overage_row = [int(overage.get(n, 0)) for n in nurses]
    jobs = [
        (
            vi,
            [situations[sid][1] for sid in keys[vi]],
            base_main[vi].tolist(),
            base_backup[vi].tolist(),
            overage_row,
            # Only measures whose bound is below some top option's value
            # can still keep the variant open.
            [
                m
                for m in ("balance", "long_term")
                if m in weighted and any(bounds[vi][m] < ref[m] for ref in reference)
            ],
        )
        for vi in to_solve
    ]
    for done, (vi, minima) in enumerate(_pool_map(_exact_minima, jobs, workers), 1):
        bounds[vi].update({m: max(bounds[vi][m], value) for m, value in minima.items()})
        status[vi] = _status(bounds[vi], reference, weighted)
        if done % 100 == 0:
            report("exact minima", done, len(jobs))

    # 5. Full evaluation of what is left, ranked with the run's candidates.
    for vi in unchecked:
        status[vi] = "unchecked"
    still_open = [vi for vi in to_solve if status[vi] == "open"]
    offset = 1 + max(idx for idx, *_ in candidates)
    items = [(offset + vi, variants[vi], tuning) for vi in still_open]
    from ..evaluation.worker import _evaluate_variant_worker

    evaluated = []
    for done, result in enumerate(_pool_map(_evaluate_variant_worker, items, workers), 1):
        evaluated.append(result)
        report("evaluate", done, len(items))
    better = []
    if evaluated:
        pool = [(idx, dict(stats), counts, df) for idx, stats, counts, df in candidates]
        pool += [(idx, dict(stats), counts, df) for idx, stats, counts, df in evaluated]
        scheduler._score_and_rank_variants(pool)
        new = {idx for idx, *_ in evaluated}
        better = [c for c in pool[:top_n] if c[0] in new]

    return VariantCheckReport(
        variants=len(variants),
        situations=len(situations),
        ruled_out=status.count("ruled out"),
        can_only_tie=status.count("tie"),
        solved=len(jobs),
        evaluated=len(evaluated),
        unchecked=len(unchecked),
        better=better,
        reference=reference,
        seconds=time.perf_counter() - began,
        statuses=status,
    )


def _measure_and_bound(scheduler, variants, tuning, workers, report):
    """Steps 1–3 for *variants* (each with its weekdays still empty).

    Returns the per-variant bounds (None where a week had too many fills to
    list), each variant's week situations, the situations' patterns, the
    base counts and the nurses.
    """
    prior, overage = scheduler._prior_violation_counts(), scheduler._historic_overage()
    nurses = list(variants[0].state.main_assignment_counts.index)
    weeks = variants[0].get_weeks()
    situation_of: dict = {}
    representative: list = []
    keys = np.zeros((len(variants), len(weeks)), dtype=np.int64)
    exact_measures: list[dict] = []
    base_main = np.zeros((len(variants), len(nurses)), dtype=np.int64)
    base_backup = np.zeros_like(base_main)
    for vi, variant in enumerate(variants):
        variant.compute_unfillable_slots()
        for w, week in enumerate(weeks):
            key = exact._situation_key(variant, week)
            if key not in situation_of:
                situation_of[key] = len(representative)
                representative.append((vi, w))
            keys[vi, w] = situation_of[key]
        schedule = variant.state.schedule
        exact_measures.append(
            {
                "rot": len(variant.rotation_violations),
                "unfillable": len(variant.unfillable_slots),
                "rot_viol": scheduler._rotation_violation_score(schedule, prior),
                "weekend_gap": scheduler._weekend_gap_penalty(schedule),
            }
        )
        base_main[vi] = [int(variant.state.main_assignment_counts[n]) for n in nurses]
        base_backup[vi] = [int(variant.state.backup_assignment_counts[n]) for n in nurses]
        if vi % 1000 == 0:
            report("measure", vi, len(variants))

    # 2. Each week situation once.
    situations = _list_situations(
        [(sid, variants[vi], w) for sid, (vi, w) in enumerate(representative)],
        tuning,
        workers,
        lambda done: report("list weeks", done, len(representative)),
    )

    # 3. Bounds from the patterns.
    bounds = _bounds(situations, keys, exact_measures, base_main, base_backup, nurses, overage)
    return bounds, keys, situations, base_main, base_backup, nurses


def _measures(scheduler, stats, counts, schedule, prior, overage) -> dict:
    """The ranking measures of one evaluated candidate."""
    return {
        "rot": int(stats["rotation_rep"]),
        "gaps": int(stats["gaps"]),
        "rot_viol": scheduler._rotation_violation_score(schedule, prior),
        "weekend_gap": scheduler._weekend_gap_penalty(schedule),
        "balance": int(stats["balance_main"]) + int(stats["balance_backup"]),
        "long_term": scheduler._long_term_score(counts, overage),
    }


def _status(bound: dict | None, reference: list[dict], weighted: list[str]) -> str:
    """ "ruled out", "tie" or "open" against every top option.

    Against one option a variant is worse when it ranks lower on the
    rank-first measures, or equals them and is no better on any weighted
    measure while worse on one; it can at best tie when it equals the option
    on all of them. Weakly worse on every weighted measure means a score at
    least as high for any positive weights and any shared normalization.
    """
    if bound is None:
        return "open"
    tie = False
    for ref in reference:
        first, ref_first = tuple(bound[k] for k in RANK_FIRST), tuple(ref[k] for k in RANK_FIRST)
        if first > ref_first:
            continue
        if first < ref_first or any(bound[m] < ref[m] for m in weighted):
            return "open"
        if all(bound[m] == ref[m] for m in weighted):
            tie = True
    return "tie" if tie else "ruled out"


def _list_situations(items, tuning, workers, report):
    """``[(empties, patterns)]`` per situation; None where a week had too many fills."""
    jobs = [
        (items[i : i + 40], tuning.exact_max_fills_per_week, tuning.exact_gap_rule_exceptions)
        for i in range(0, len(items), 40)
    ]
    out: dict = {}
    for part in _pool_map(_list_chunk, jobs, workers):
        out.update(part)
        report(len(out))
    return [out[sid] for sid in range(len(items))]


def _list_chunk(job):
    items, max_fills, mode = job
    out = {}
    for sid, variant, w in items:
        probe = variant.clone()
        try:
            options = exact._week_options(probe, probe.get_weeks()[w], max_fills, mode)
        except exact._TooManyFills:
            out[sid] = None
            continue
        out[sid] = (options.empties, np.array(list(options.fills), dtype=np.int16))
    return out


def _bounds(situations, keys, measures, base_main, base_backup, nurses, overage):
    """Exact unfilled slots and lower bounds on balance and fairness, per variant."""
    n = len(nurses)
    count = len(situations)
    lo_m, hi_m, lo_b, hi_b, lo_t, hi_t = (np.zeros((count, n), dtype=np.int64) for _ in range(6))
    fill_range = np.zeros((count, 4), dtype=np.int64)
    empties = np.zeros(count, dtype=np.int64)
    missing = np.zeros(count, dtype=bool)
    for sid, situation in enumerate(situations):
        if situation is None:
            missing[sid] = True
            continue
        empties[sid], patterns = situation
        main, backup = patterns[:, :n].astype(np.int64), patterns[:, n : 2 * n].astype(np.int64)
        lo_m[sid], hi_m[sid] = main.min(0), main.max(0)
        lo_b[sid], hi_b[sid] = backup.min(0), backup.max(0)
        lo_t[sid], hi_t[sid] = (main + backup).min(0), (main + backup).max(0)
        fill_range[sid] = [main.sum(1).min(), main.sum(1).max()] + [
            backup.sum(1).min(),
            backup.sum(1).max(),
        ]
    over = np.array([int(overage.get(nurse, 0)) for nurse in nurses], dtype=np.int64)
    out = []
    for vi, ids in enumerate(keys):
        if missing[ids].any():
            out.append(None)
            continue
        bm, bb = base_main[vi], base_backup[vi]
        fills = fill_range[ids].sum(0)
        main_total = (int(bm.sum() + fills[0]), int(bm.sum() + fills[1]))
        backup_total = (int(bb.sum() + fills[2]), int(bb.sum() + fills[3]))
        balance = _spread_bound(
            backup_total, bb + lo_b[ids].sum(0), bb + hi_b[ids].sum(0), nurses
        ) + _spread_bound(main_total, bm + lo_m[ids].sum(0), bm + hi_m[ids].sum(0), nurses)
        total_lo, total_hi = bm + bb + lo_t[ids].sum(0), bm + bb + hi_t[ids].sum(0)
        # long_term = sum max(0, overage + total - min total), and min total
        # is at most the smallest most-possible total.
        most_min = int(total_hi.min())
        shifts = main_total[0] + backup_total[0]
        long_term = max(
            int(np.maximum(0, over + total_lo - most_min).sum()),
            int(over.sum()) + shifts - n * most_min,
            int(np.maximum(0, over).sum()),
        )
        out.append(
            {
                "rot": measures[vi]["rot"],
                "gaps": measures[vi]["unfillable"] + int(empties[ids].sum()),
                "rot_viol": measures[vi]["rot_viol"],
                "weekend_gap": measures[vi]["weekend_gap"],
                "balance": balance,
                "long_term": long_term,
            }
        )
    return out


def _spread_bound(total_range, lo, hi, nurses) -> int:
    """A lower bound on max - min of counts each within [lo, hi]."""
    bound = max(0, int(lo.max() - hi.min()))
    if total_range[0] == total_range[1]:
        fixed = dict(zip(nurses, lo.tolist(), strict=True))
        caps = dict(zip(nurses, hi.tolist(), strict=True))
        bound = max(bound, spread_lower_bound_for_total(total_range[0], nurses, fixed, caps))
    return bound


def _exact_minima(job):
    """The least balance and fairness penalty any legal fill gives one variant."""
    from ortools.sat.python import cp_model

    vi, week_patterns, base_main, base_backup, overage, wanted = job
    n = len(base_main)
    minima = {}
    for measure in wanted:
        model = cp_model.CpModel()
        main_terms, backup_terms = [[] for _ in range(n)], [[] for _ in range(n)]
        for patterns in week_patterns:
            picks = [model.NewBoolVar("") for _ in range(len(patterns))]
            for i in range(n):
                for column, terms in ((i, main_terms[i]), (n + i, backup_terms[i])):
                    for j in np.nonzero(patterns[:, column])[0]:
                        terms.append(int(patterns[j, column]) * picks[j])
            model.AddExactlyOne(picks)
        top = 4 * sum(len(p) for p in week_patterns) + 400
        mains = [model.NewIntVar(0, top, "") for _ in range(n)]
        backups = [model.NewIntVar(0, top, "") for _ in range(n)]
        for i in range(n):
            model.Add(mains[i] == base_main[i] + sum(main_terms[i]))
            model.Add(backups[i] == base_backup[i] + sum(backup_terms[i]))
        if measure == "balance":
            spreads = []
            for values in (mains, backups):
                high, low = model.NewIntVar(0, top, ""), model.NewIntVar(0, top, "")
                for value in values:
                    model.Add(value <= high)
                    model.Add(value >= low)
                spreads.append(high - low)
            model.Minimize(sum(spreads))
        else:
            totals = [mains[i] + backups[i] for i in range(n)]
            least = model.NewIntVar(0, 2 * top, "")
            excess = []
            for i in range(n):
                model.Add(least <= totals[i])
                term = model.NewIntVar(0, 4 * top + 1000, "")
                model.Add(term >= overage[i] + totals[i] - least)
                excess.append(term)
            model.Minimize(sum(excess))
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.cp_model_presolve = False
        solver.parameters.cp_model_probing_level = 0
        solver.parameters.max_time_in_seconds = 120
        status = solver.Solve(model)
        # Only a proven bound may rule a variant out; otherwise keep it open.
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            minima[measure] = int(round(solver.BestObjectiveBound()))
    return vi, minima


def _pool_map(fn, jobs, workers):
    """``map`` in order over a spawn-started process pool.

    Workers do not inherit the parent's memory (all the variants), and only
    a few jobs per worker are queued at a time, since a job can carry a
    variant or its weeks' patterns.
    """
    if not jobs:
        return iter(())
    if workers <= 1:
        return map(fn, jobs)
    context = multiprocessing.get_context("spawn")

    def run():
        with ProcessPoolExecutor(workers, mp_context=context) as pool:
            pending = deque()
            for job in jobs:
                pending.append(pool.submit(fn, job))
                if len(pending) >= 4 * workers:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()

    return run()


__all__ = ["VariantCheckReport", "check_all_weekend_variants"]
