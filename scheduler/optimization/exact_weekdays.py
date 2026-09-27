"""Exact weekday assignment for one weekend variant.

Once a variant's weekends are fixed, which weekday fills are legal in one
Mon–Thu week does not depend on the other weeks, as long as no rule reaches
past the weekend around it: with ``min_days_between_assignments`` at most 3,
spacing from Monday back and from Thursday forward only ever lands on fixed
weekend days, and the weekly limits stay inside the week. The weekday problem
is then "choose one fill per week", coupled only through the per-nurse counts
the quality measures read.

:func:`solve_weekdays_exactly` lists every legal fill of each week with the
scheduler's own eligibility checks, so the rules have one implementation,
and lets OR-Tools CP-SAT choose one fill per week that minimizes, in order,
the keys the local search optimizes (``ScheduleQuality.compare_to``):
unfilled slots, backup spread, main spread, total spread, and the long-term
history penalty. Rotation repeats and weekend spacing are fixed by the
weekends, so they do not vary here. The chosen fills are then placed through
the normal assignment checks, which re-verifies every one of them.

A week is filled under the strictest rules that can fill it: the ordinary
weekday rules first, then the one-day spacing relaxation if it is enabled,
then the gap-filling rules (which also allow a Tuesday before a worked
weekend), and only if none of those fills the week, the fills that leave the
fewest slots empty. So a relaxation is used only in a week that needs it for
coverage, as the search intends.

It returns None, leaving the variant untouched, whenever it cannot promise
an exact answer: OR-Tools is not installed, the spacing rule couples weeks,
or a week has more legal fills than ``max_fills_per_week``. Callers then use
the local search.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..domain import spread_lower_bound_for_total

logger = logging.getLogger(__name__)

ROLES = ("main", "backup")
# Beyond this, spacing can reach from one week's weekday into the next's.
MAX_INDEPENDENT_SPACING = 3


@dataclass
class ExactOutcome:
    """What :func:`solve_weekdays_exactly` did."""

    optimal: bool  # CP-SAT proved every key optimal within the time limit
    gaps: int  # fillable weekday slots left empty (fewest possible)
    # Mondays of the weeks whose chosen fill uses a gap-rule exception or the
    # one-day spacing relaxation, or leaves fillable slots empty.
    relaxed_weeks: list[str] = field(default_factory=list)
    patterns: int = 0  # distinct per-week count patterns the solver chose among


class _TooManyFills(Exception):
    pass


@dataclass
class _WeekOptions:
    slots: list  # [(date, role)] the week's modifiable slots, in order
    gap_mode: bool
    relaxed: bool
    empties: int  # slots each fill leaves empty (the fewest possible)
    fills: dict  # count pattern -> first fill with it (tuple of nurse or None)


def unavailable_reason(variant) -> str | None:
    """Why the exact solve cannot promise an exact answer here, or None."""
    spacing = int(getattr(variant.config, "min_days_between_assignments", 0) or 0)
    if spacing > MAX_INDEPENDENT_SPACING:
        return (
            f"min_days_between_assignments={spacing} lets spacing reach across a weekend, "
            "so weeks are not independent"
        )
    try:
        import ortools.sat.python.cp_model  # noqa: F401
    except ImportError:
        return "OR-Tools is not installed"
    return None


def solve_weekdays_exactly(
    variant,
    *,
    time_limit_ms: int = 20_000,
    max_fills_per_week: int = 200_000,
    objective: str = "balanced",
    gap_rule_exceptions: str = "for_balance",
) -> ExactOutcome | None:
    """Fill the variant's empty weekday slots optimally, or return None.

    Call it once the weekends are fixed and ``compute_unfillable_slots`` has
    run, with the weekday slots still empty (pinned cells aside). On None the
    variant is unchanged.
    """
    reason = unavailable_reason(variant)
    if reason is not None:
        logger.info("Exact weekday solve not used: %s", reason)
        return None
    from ortools.sat.python import cp_model

    probe = variant.clone()
    weeks = variant.get_weeks()
    try:
        options = [
            _week_options(probe, week, max_fills_per_week, gap_rule_exceptions) for week in weeks
        ]
    except _TooManyFills:
        logger.info(
            "Exact weekday solve not used: a week has more than %d legal fills",
            max_fills_per_week,
        )
        return None

    nurses = list(probe.state.main_assignment_counts.index)
    base_main = [int(probe.state.main_assignment_counts[n]) for n in nurses]
    base_backup = [int(probe.state.backup_assignment_counts[n]) for n in nurses]

    model = cp_model.CpModel()
    choice = []
    main_terms: list[list] = [[] for _ in nurses]
    backup_terms: list[list] = [[] for _ in nurses]
    exception_terms: list = []
    for w, week in enumerate(options):
        picks = []
        for p, pattern in enumerate(week.fills):
            x = model.NewBoolVar(f"w{w}p{p}")
            picks.append((x, pattern))
            for i in range(len(nurses)):
                if pattern[i]:
                    main_terms[i].append(pattern[i] * x)
                if pattern[len(nurses) + i]:
                    backup_terms[i].append(pattern[len(nurses) + i] * x)
            if pattern[-1]:
                exception_terms.append(pattern[-1] * x)
        model.AddExactlyOne(x for x, _ in picks)
        choice.append(picks)

    horizon = len(variant.state.frame_index) * 2 + 1
    mains = [model.NewIntVar(0, horizon, f"main_{i}") for i in range(len(nurses))]
    backups = [model.NewIntVar(0, horizon, f"backup_{i}") for i in range(len(nurses))]
    totals = [model.NewIntVar(0, 2 * horizon, f"total_{i}") for i in range(len(nurses))]
    for i in range(len(nurses)):
        model.Add(mains[i] == base_main[i] + sum(main_terms[i]))
        model.Add(backups[i] == base_backup[i] + sum(backup_terms[i]))
        model.Add(totals[i] == mains[i] + backups[i])

    def spread(values, name):
        # Every count between a low and a high mark; minimizing high - low
        # makes it max - min exactly. Linear bounds like these solve far
        # faster than max/min equalities.
        high = model.NewIntVar(0, 2 * horizon, f"{name}_high")
        low = model.NewIntVar(0, 2 * horizon, f"{name}_low")
        for value in values:
            model.Add(value <= high)
            model.Add(value >= low)
        return high - low

    backup_spread, main_spread = spread(backups, "backup"), spread(mains, "main")
    total_spread = spread(totals, "total")
    # Proven lower bounds (see ScheduleVariant.spread_lower_bounds). CP-SAT
    # cannot easily see, say, that 28 shifts cannot split evenly among 9
    # nurses; with these it proves a schedule optimal as soon as it reaches
    # them. Every week fills the same number of slots in every one of its
    # fills, so each role's total is known up front.
    fixed, caps = probe._spread_bound_inputs()
    first = [options_i[0] for options_i in (list(week.fills) for week in options) if options_i]
    main_total = sum(base_main) + sum(sum(p[: len(nurses)]) for p in first)
    backup_total = sum(base_backup) + sum(sum(p[len(nurses) : 2 * len(nurses)]) for p in first)
    model.Add(
        backup_spread
        >= spread_lower_bound_for_total(backup_total, nurses, fixed["backup"], caps["backup"])
    )
    model.Add(
        main_spread >= spread_lower_bound_for_total(main_total, nurses, fixed["main"], caps["main"])
    )
    model.Add(
        total_spread
        >= spread_lower_bound_for_total(
            main_total + backup_total, nurses, fixed["total"], caps["total"]
        )
    )
    if objective == "balanced":
        larger = model.NewIntVar(0, 2 * horizon, "larger_spread")
        model.Add(larger >= backup_spread)
        model.Add(larger >= main_spread)
        keys = [backup_spread + main_spread, larger, total_spread]
    else:
        keys = [backup_spread, main_spread, total_spread]
    # Gap-rule exceptions (a Tuesday right before the nurse's weekend) only
    # where they improve the spreads.
    keys.append(sum(exception_terms))
    overage = getattr(variant, "historic_overage", None)
    if overage:
        # long_term_score: sum over nurses of max(0, overage + total - min total).
        # min_total may sit at or below the true minimum; raising it never
        # increases the sum, so at the optimum it is the true minimum.
        min_total = model.NewIntVar(0, 2 * horizon, "min_total")
        for total in totals:
            model.Add(min_total <= total)
        excess = []
        for i, nurse in enumerate(nurses):
            term = model.NewIntVar(0, 4 * horizon + 1000, f"excess_{i}")
            model.Add(term >= int(overage.get(nurse, 0)) + totals[i] - min_total)
            excess.append(term)
        keys.append(sum(excess))

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1  # already one process per variant
    solver.parameters.random_seed = 0
    # CP-SAT's presolve and probing spend seconds on this model (thousands
    # of fill choices feeding a few per-nurse sums) before searching; without
    # them each key solves and proves in a fraction of the time.
    solver.parameters.cp_model_presolve = False
    solver.parameters.cp_model_probing_level = 0
    optimal = True
    solution = None
    # The limit covers the solver; enumeration is bounded by max_fills_per_week.
    deadline = time.perf_counter() + time_limit_ms / 1000.0
    for key in keys:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            optimal = False
            break
        solver.parameters.max_time_in_seconds = remaining
        model.Minimize(key)
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            optimal = False
            break
        solution = [
            next(pattern for x, pattern in picks if solver.BooleanValue(x)) for picks in choice
        ]
        value = int(round(solver.ObjectiveValue()))
        if status != cp_model.OPTIMAL:
            optimal = False
            break
        # Keep this key at its optimum while the later keys are minimized,
        # starting from the whole solution just found.
        model.Add(key == value)
        model.ClearHints()
        for index in range(len(model.Proto().variables)):
            var = model.GetIntVarFromProtoIndex(index)
            model.AddHint(var, solver.Value(var))
    if solution is None:
        logger.info("Exact weekday solve found no solution within its time limit")
        return None

    placed: list[tuple] = []
    for week, pattern in zip(options, solution, strict=True):
        fill = week.fills[pattern]
        for (day, role), nurse in zip(week.slots, fill, strict=True):
            if nurse is None:
                continue
            if not variant.inc_assign(day, role, nurse, gap_phase=week.gap_mode):
                # Weeks were not independent after all: undo, and let the
                # search handle this variant.
                logger.warning(
                    "Exact weekday solve: %s %s for %s failed its re-check; using the search",
                    day.date(),
                    role,
                    nurse,
                )
                for d, r, n in reversed(placed):
                    variant.dec_assign(d, r, n)
                variant.recalculate_assignment_counts()
                return None
            placed.append((day, role, nurse))
    variant.recalculate_assignment_counts()

    return ExactOutcome(
        optimal=optimal,
        gaps=sum(week.empties for week in options),
        relaxed_weeks=[
            f"{min(week.slots)[0].date()}"
            for week, pattern in zip(options, solution, strict=True)
            if week.slots and (pattern[-1] or week.relaxed or week.empties)
        ],
        patterns=sum(len(week.fills) for week in options),
    )


def _week_options(probe, week, max_fills: int, gap_rule_exceptions: str) -> _WeekOptions:
    """Every legal fill of one week, under the strictest rules that fill it.

    With ``gap_rule_exceptions="for_balance"`` the gap-filling rules are
    allowed in every week, and the solver keeps their exceptions to the
    fewest that give the best spreads.
    """
    slots = [
        (day, role)
        for day in week
        for role in ROLES
        if not probe.is_pre_scheduled(day, role) and not probe.is_unfillable(day, role)
    ]
    relax = bool(probe.config.one_day_gap_enabled)
    tiers = [] if gap_rule_exceptions == "for_balance" else [(False, False)]
    if relax and gap_rule_exceptions != "for_balance":
        tiers.append((False, True))
    tiers.append((True, False))
    if relax:
        tiers.append((True, True))

    for gap_mode, relaxed in tiers:
        fills = _enumerate(probe, slots, gap_mode, relaxed, max_fills, max_empty=0)
        if fills:
            return _WeekOptions(slots, gap_mode, relaxed, 0, fills)
    gap_mode, relaxed = tiers[-1]
    for empties in range(1, len(slots) + 1):
        fills = _enumerate(probe, slots, gap_mode, relaxed, max_fills, max_empty=empties)
        if fills:
            return _WeekOptions(slots, gap_mode, relaxed, empties, fills)
    return _WeekOptions(slots, gap_mode, relaxed, len(slots), {})  # pragma: no cover


def _enumerate(probe, slots, gap_mode: bool, relaxed: bool, max_fills: int, *, max_empty: int):
    """``{count pattern: first fill}`` over every legal fill of *slots*.

    A fill leaves at most *max_empty* slots empty. Each nurse is checked
    against the slots already filled, with the scheduler's eligibility
    rules; the ordinary rules are symmetric between two shifts, and fills
    made under the one-day relaxation, whose applicability depends on the
    day, are re-checked in full.
    """
    if gap_mode:
        # Probes, not assignment decisions: keep them out of the debug log.
        def eligible(day, role, *, relaxed_spacing):
            return probe.get_eligible_nurses_for_day_gap(
                day, role, relaxed_spacing=relaxed_spacing, log=False
            )
    else:
        eligible = probe.get_eligible_nurses_for_day
    nurses = list(probe.state.main_assignment_counts.index)
    index = {nurse: i for i, nurse in enumerate(nurses)}
    n = len(nurses)
    fills: dict[tuple, tuple] = {}
    count = [0]
    nodes = [0]
    current: list = []
    # Whether a shift is a gap-rule exception depends only on the nurse and
    # the day (their fixed weekends), not on the rest of the fill.
    exceptions: dict[tuple, bool] = {}

    def is_exception(nurse, day, role) -> bool:
        key = (nurse, day, role)
        found = exceptions.get(key)
        if found is None:
            found = exceptions[key] = probe.is_gap_rule_exception(nurse, day, role)
        return found

    # Trial placements touch only the cells: the eligibility rules read the
    # grid, never the counts, and the patterns are counted from the fill.
    grid = probe.state.grid()

    def complete() -> None:
        count[0] += 1
        if count[0] > max_fills:
            raise _TooManyFills
        if relaxed and not _recheck(probe, slots, current, eligible):
            return
        # Per-nurse Main and Backup counts, then the gap-rule exceptions.
        pattern = [0] * (2 * n + 1)
        for (day, role), nurse in zip(slots, current, strict=True):
            if nurse is not None:
                pattern[index[nurse] + (0 if role == "main" else n)] += 1
                if gap_mode and is_exception(nurse, day, role):
                    pattern[-1] += 1
        fills.setdefault(tuple(pattern), tuple(current))

    def dfs(k: int, empties: int) -> None:
        if k == len(slots):
            if empties == max_empty:
                complete()
            return
        nodes[0] += 1
        if nodes[0] > 50 * max_fills:
            raise _TooManyFills
        day, role = slots[k]
        for nurse in eligible(day, role, relaxed_spacing=relaxed):
            if nurse not in index:
                continue
            # Eligible in this very state, so place it without re-checking.
            grid.set(day, role, nurse)
            current.append(nurse)
            dfs(k + 1, empties)
            current.pop()
            grid.set(day, role, None)
        if empties < max_empty:
            current.append(None)
            dfs(k + 1, empties + 1)
            current.pop()

    dfs(0, 0)
    return fills


def _recheck(probe, slots, fill, eligible) -> bool:
    """Every nurse in a complete fill passes the rules with the others in place."""
    ok = True
    grid = probe.state.grid()
    for (day, role), nurse in zip(slots, fill, strict=True):
        if nurse is None:
            continue
        grid.set(day, role, None)
        ok = nurse in eligible(day, role, relaxed_spacing=True)
        grid.set(day, role, nurse)
        if not ok:
            break
    return ok


__all__ = ["ExactOutcome", "solve_weekdays_exactly", "unavailable_reason"]
