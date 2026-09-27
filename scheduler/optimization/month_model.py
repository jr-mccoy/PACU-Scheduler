"""The whole month as one CP-SAT model: weekends and weekdays together.

A run branches on weekend variants, keeps at most ``max_weekend_variants`` of
them, and fills each one's weekdays. This model instead encodes the
scheduler's rules as constraints over every cell of the month at once, so
CP-SAT searches every weekend arrangement without listing them and proves
the best month; no cap is involved.

Variables: for each generated weekend and nurse, whether they work it as FSF
or as SFS; for each modifiable weekday slot and nurse, whether they take it.

Rules, each mirroring the scheduler's check of the same name:

* weekends (``_get_valid_nurse_pairs``): one FSF and one SFS nurse; all
  three days available (``_is_nurse_eligible_for_weekend``, which also
  covers the weekend gap against history, pinned and recorded weekends);
  the weekend gap between the month's own weekends; strict FSF/SFS
  alternation; pinned cells, which override the filters as the generator
  does (a pinned repeat is counted); no late/late pair;
* weekdays (``get_eligible_nurses_for_day_gap``): availability, one role a
  day, the late-shift pair rule, spacing against every shift in reach
  (weekends and days just outside the window included), the weekly limits,
  and the weekend windows: no Monday or Tuesday after one's weekend and the
  Wednesday/Thursday settings, no Wednesday or Thursday before it, and a
  Tuesday before it allowed but counted, as the exact weekday solve's
  "for_balance" policy does.

It minimizes, in order (``MONTH_ORDER``): rotation repeats, unfilled slots,
the rotation score, the weekend spacing penalty (``_weekend_gap_penalty``,
reproduced exactly), main + backup spread, the larger of the two, long-term
fairness, total spread, Tuesday exceptions, and same-weekday repeats. The
first four and fairness are what the ranking weighs; the order among them is
fixed here, where the ranking's weighted score is normalized over a pool.
``top_n`` asks for further months with different weekends.

:func:`replay` places a solution through the scheduler's own checks and
recomputes every measure; use it to verify a solution.

Not modelled (``unsupported`` says why): the one-day spacing relaxation,
allowing rotation repeats, and a PRN nurse pinned into a weekend.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from scheduler.domain import (
    MAX_MAIN_ASSIGNMENTS_PER_WEEK,
    MAX_TOTAL_ASSIGNMENTS_PER_WEEK,
    ScheduleVariant,
    WeekendPattern,
)
from scheduler.runtime import is_empty

ROLES = ("main", "backup")
FSF, SFS = WeekendPattern.FSF, WeekendPattern.SFS
# Cells of a weekend: (day offset, role) -> pattern whose nurse holds it.
WEEKEND_CELLS = {
    (0, "main"): FSF,
    (0, "backup"): SFS,
    (1, "main"): SFS,
    (1, "backup"): FSF,
    (2, "main"): FSF,
    (2, "backup"): SFS,
}

MONTH_ORDER = (
    "rotation",
    "gaps",
    "rot_viol",
    "weekend_gap",
    "balance",
    "larger",
    "long_term",
    "total",
    "exceptions",
    "repeats",
)
# The exact weekday solve's order, for comparing with it.
WEEKDAY_ORDER = ("gaps", "balance", "larger", "total", "exceptions", "long_term")


@dataclass
class MonthSolution:
    weekends: dict  # friday -> (fsf, sfs)
    weekdays: dict  # (day, role) -> nurse, the modifiable weekday cells only
    values: dict  # objective key -> value
    optimal: bool
    seconds: float
    status: list = field(default_factory=list)


def root_variant(scheduler) -> ScheduleVariant:
    """The variant generation starts from (no weekend assigned yet)."""
    return ScheduleVariant(
        scheduler.get_state_snapshot(),
        scheduler.nurses,
        scheduler.availability,
        scheduler.config,
        scheduler.nurse_manager,
        scheduler._collect_pre_scheduled_slots(),
        historical_main=scheduler._historical_main,
        historical_backup=scheduler._historical_backup,
        historic_overage=scheduler._historic_overage(),
    )


def unsupported(scheduler) -> str | None:
    """Why the model cannot represent this scheduler's rules, or None."""
    if scheduler.config.one_day_gap_enabled:
        return "the one-day spacing relaxation is not modelled"
    nurses = set(scheduler.nurses)
    for roles in scheduler._get_pre_scheduled_weekend_assignments().values():
        for nurse in (roles or {}).values():
            if nurse and nurse not in nurses:
                return f"{nurse} is pinned into a weekend but is not scheduled automatically"
    return None


def solve_month(
    scheduler,
    *,
    top_n: int = 1,
    order=MONTH_ORDER,
    fixed_weekends: dict | None = None,
    time_limit_s: float = 600.0,
    workers: int = 8,
    log: bool = False,
) -> list[MonthSolution]:
    """The best ``top_n`` months, each with different weekends."""
    if unsupported(scheduler):
        return []
    solutions: list[MonthSolution] = []
    excluded: list[dict] = []
    for _ in range(top_n):
        built = _Model(scheduler, fixed_weekends, excluded)
        found = built.solve(order, time_limit_s, workers, log)
        if found is None:
            break
        solutions.append(found)
        excluded.append(found.weekends)
    return solutions


class _Model:
    def __init__(self, scheduler, fixed_weekends, excluded):
        from ortools.sat.python import cp_model

        self.cp = cp_model
        self.s = scheduler
        self.root = root_variant(scheduler)
        root = self.root
        self.m = model = cp_model.CpModel()
        self.nurses = list(root.nurses)
        nurses = self.nurses
        cfg = scheduler.config
        self.days = list(root.state.frame_index)
        index = set(self.days)
        weekend_flag = dict(zip(self.days, root.state.weekend_flags(), strict=True))
        base = root.state.schedule  # pinned cells seeded
        self.fridays = list(scheduler._get_weekends())
        pre = scheduler._get_pre_scheduled_weekend_assignments()
        generated_days = {f + timedelta(days=i): f for f in self.fridays for i in range(3)}
        late = set(root._late_set)
        start = scheduler.start_date

        def pinned(day, role):
            value = base.at[day, role]
            return None if is_empty(value) else value

        # ── weekends ──────────────────────────────────────────────────────
        self.fsf, self.sfs = {}, {}
        self.pinned_weekend = {}  # (friday, nurse) -> pattern forced by a pin
        for f in self.fridays:
            fixed = pre.get(f, {}) or {}
            forced = {}
            for pattern in (FSF, SFS):
                if fixed.get(pattern):
                    forced[pattern] = fixed[pattern]
            for (offset, role), pattern in WEEKEND_CELLS.items():
                day = f + timedelta(days=offset)
                if day in index and pinned(day, role):
                    forced.setdefault(pattern, pinned(day, role))
                    if forced[pattern] != pinned(day, role):
                        model.AddBoolOr([])  # contradictory pins: no solution
            dates_in_idx = [
                d for d in scheduler._weekend_dates(f) if d in scheduler.availability.index
            ]
            for n in nurses:
                eligible = scheduler._is_nurse_eligible_for_weekend(n, dates_in_idx, f, base, pre)
                for pattern, table in ((FSF, self.fsf), (SFS, self.sfs)):
                    var = model.NewBoolVar(f"{pattern.value}_{f.date()}_{n}")
                    table[f, n] = var
                    if forced.get(pattern) == n:
                        model.Add(var == 1)
                        self.pinned_weekend[f, n] = pattern
                    elif forced.get(pattern) is not None or not eligible:
                        model.Add(var == 0)
            model.AddExactlyOne(self.fsf[f, n] for n in nurses)
            model.AddExactlyOne(self.sfs[f, n] for n in nurses)
            for n in nurses:
                model.Add(self.fsf[f, n] + self.sfs[f, n] <= 1)
            both_pinned = FSF in forced and SFS in forced
            if not both_pinned:
                for a, b in itertools.permutations(late, 2):
                    if a in nurses and b in nurses:
                        model.Add(self.fsf[f, a] + self.sfs[f, b] <= 1)
        self.work = {(f, n): self.fsf[f, n] + self.sfs[f, n] for f in self.fridays for n in nurses}

        # Weekend gap between the month's own weekends.
        gap_min = cfg.weekend_gap_days
        for f1, f2 in itertools.combinations(self.fridays, 2):
            if (f2 - f1).days < gap_min:
                for n in nurses:
                    if (f1, n) in self.pinned_weekend and (f2, n) in self.pinned_weekend:
                        continue
                    model.Add(self.work[f1, n] + self.work[f2, n] <= 1)

        # Strict alternation; a pinned weekend may repeat (counted).
        last = dict(root.state.last_pattern)
        self.violations = []  # (indicator, nurse)
        for li, f2 in enumerate(self.fridays):
            for n in nurses:
                before = [self.work[f, n] for f in self.fridays[:li]]
                for pattern, table in ((FSF, self.fsf), (SFS, self.sfs)):
                    exprs = []
                    if last.get(n) == pattern:
                        exprs.append(1 - sum(before))
                    for ki, f1 in enumerate(self.fridays[:li]):
                        between = [self.work[f, n] for f in self.fridays[ki + 1 : li]]
                        exprs.append(table[f1, n] - sum(between))
                    if not exprs:
                        continue
                    if self.pinned_weekend.get((f2, n)) == pattern:
                        v = model.NewBoolVar("")
                        for e in exprs:
                            model.Add(v >= e + table[f2, n] - 1)
                        self.violations.append((v, n))
                    else:
                        for e in exprs:
                            model.Add(e + table[f2, n] <= 1)

        if fixed_weekends:
            for f, (a, b) in fixed_weekends.items():
                model.Add(self.fsf[f, a] == 1)
                model.Add(self.sfs[f, b] == 1)
        for other in excluded:
            chosen = [self.fsf[f, a] for f, (a, b) in other.items()]
            chosen += [self.sfs[f, b] for f, (a, b) in other.items()]
            model.Add(sum(chosen) <= len(chosen) - 1)

        # ── weekdays ──────────────────────────────────────────────────────
        self.x = {}
        weekdays = [d for d in self.days if not weekend_flag[d]]
        self.slots = [(d, r) for d in weekdays for r in ROLES if not root._is_pre_scheduled(d, r)]
        slot_set = set(self.slots)
        for d, r in self.slots:
            other = "backup" if r == "main" else "main"
            other_pinned = pinned(d, other) if (d, other) not in slot_set else None
            for n in nurses:
                if not root._is_available(n, d) or other_pinned == n:
                    continue
                if n in late and other_pinned in late:
                    continue
                self.x[d, r, n] = model.NewBoolVar(f"x_{d.date()}_{r}_{n}")
            model.AddAtMostOne(self.x[d, r, n] for n in nurses if (d, r, n) in self.x)
        for d in weekdays:
            for n in nurses:
                cells = [self.x[d, r, n] for r in ROLES if (d, r, n) in self.x]
                if len(cells) > 1:
                    model.AddAtMostOne(cells)
            if (d, "main") in slot_set and (d, "backup") in slot_set:
                for a, b in itertools.permutations(late, 2):
                    if (d, "main", a) in self.x and (d, "backup", b) in self.x:
                        model.Add(self.x[d, "main", a] + self.x[d, "backup", b] <= 1)

        def works(day, n):
            """Linear expression: n works *day* (0/1), or a constant."""
            if day not in index:
                return None
            if day in generated_days:
                f = generated_days[day]
                offset = (day - f).days
                return sum(
                    (self.fsf if WEEKEND_CELLS[offset, r] == FSF else self.sfs)[f, n] for r in ROLES
                )
            total = 0
            for r in ROLES:
                if (day, r) in slot_set:
                    if (day, r, n) in self.x:
                        total += self.x[day, r, n]
                elif pinned(day, r) == n:
                    total += 1
            return total

        self.x_day = {}
        for d in weekdays:
            for n in nurses:
                cells = [self.x[d, r, n] for r in ROLES if (d, r, n) in self.x]
                if cells:
                    self.x_day[d, n] = sum(cells)

        # Spacing, against every shift within reach (and just outside the window).
        spacing = int(cfg.min_days_between_assignments)
        for (d, n), xd in self.x_day.items():
            outside = root._worked_outside_window(n)
            for gap in range(1, spacing + 1):
                for other in (d - timedelta(days=gap), d + timedelta(days=gap)):
                    if other not in index:
                        if other in outside:
                            model.Add(xd == 0)
                        continue
                    if other > d and (other, n) in self.x_day:
                        model.Add(xd + self.x_day[other, n] <= 1)  # once per weekday pair
                        continue
                    if (other, n) in self.x_day:
                        continue
                    w = works(other, n)
                    if isinstance(w, int):
                        if w:
                            model.Add(xd == 0)
                    else:
                        model.Add(xd + w <= 1)

        # Weekly limits (Mon–Thu).
        for week in root.get_weeks():
            week = [d for d in week if d in index and not weekend_flag[d]]
            for n in nurses:
                pin_main = sum(
                    1 for d in week if (d, "main") not in slot_set and pinned(d, "main") == n
                )
                pin_total = pin_main + sum(
                    1 for d in week if (d, "backup") not in slot_set and pinned(d, "backup") == n
                )
                mains = [self.x[d, "main", n] for d in week if (d, "main", n) in self.x]
                alls = [self.x_day[d, n] for d in week if (d, n) in self.x_day]
                if pin_main >= MAX_MAIN_ASSIGNMENTS_PER_WEEK:
                    for v in mains:
                        model.Add(v == 0)
                elif mains:
                    model.Add(sum(mains) + pin_main <= MAX_MAIN_ASSIGNMENTS_PER_WEEK)
                if pin_total >= MAX_TOTAL_ASSIGNMENTS_PER_WEEK:
                    for v in alls:
                        model.Add(v == 0)
                elif alls:
                    model.Add(sum(alls) + pin_total <= MAX_TOTAL_ASSIGNMENTS_PER_WEEK)

        # Weekend windows. A nurse's neighbouring weekends are the month's
        # weekends they work plus fixed ones (history, recorded, pinned cuts).
        static_fridays = {
            n: set(root.state.nurse_weekend_lists.get(n, [])) - set(self.fridays) for n in nurses
        }
        post = root._weekday_constraint_config()
        self.exceptions = []
        for (d, r, n), var in self.x.items():
            weekday = d.weekday()
            prev_f = d - timedelta(days=weekday + 3)
            next_f = d + timedelta(days=4 - weekday)
            allowed_after = {
                2: post.allow_post_weekend_wednesday_main
                if r == "main"
                else post.allow_post_weekend_wednesday_backup,
                3: post.allow_post_weekend_thursday_main
                if r == "main"
                else post.allow_post_weekend_thursday_backup,
            }.get(weekday, False)

            def neighbour(friday, n=n):
                if friday in static_fridays[n]:
                    return 1
                if friday in self.fridays:
                    return self.work[friday, n]
                return 0

            after = neighbour(prev_f)
            if not allowed_after:
                if isinstance(after, int):
                    if after:
                        model.Add(var == 0)
                else:
                    model.Add(var + after <= 1)
            ahead = neighbour(next_f)
            if weekday in (2, 3):
                if isinstance(ahead, int):
                    if ahead:
                        model.Add(var == 0)
                else:
                    model.Add(var + ahead <= 1)
            elif weekday == 1:  # the gap-filling Tuesday: allowed, counted
                if isinstance(ahead, int):
                    if ahead:
                        self.exceptions.append(var)
                else:
                    e = model.NewBoolVar("")
                    model.Add(e >= var + ahead - 1)
                    self.exceptions.append(e)

        # ── measures ──────────────────────────────────────────────────────
        H = 4 * len(self.days) + 10
        main_count, backup_count = {}, {}
        for n in nurses:
            m = sum(2 * self.fsf[f, n] + self.sfs[f, n] for f in self.fridays)
            b = sum(self.fsf[f, n] + 2 * self.sfs[f, n] for f in self.fridays)
            for day in self.days:
                if day in generated_days:
                    continue
                for r in ROLES:
                    if (day, r) in slot_set:
                        if (day, r, n) in self.x:
                            if r == "main":
                                m += self.x[day, r, n]
                            else:
                                b += self.x[day, r, n]
                    elif pinned(day, r) == n:
                        if r == "main":
                            m += 1
                        else:
                            b += 1
            mv, bv = model.NewIntVar(0, H, ""), model.NewIntVar(0, H, "")
            model.Add(mv == m)
            model.Add(bv == b)
            main_count[n], backup_count[n] = mv, bv
        totals = {}
        for n in nurses:
            t = model.NewIntVar(0, 2 * H, "")
            model.Add(t == main_count[n] + backup_count[n])
            totals[n] = t
        self.main_count, self.backup_count, self.totals = main_count, backup_count, totals

        def spread(values):
            hi, lo = model.NewIntVar(0, 2 * H, ""), model.NewIntVar(0, 2 * H, "")
            for v in values:
                model.Add(v <= hi)
                model.Add(v >= lo)
            return hi - lo

        sb, sm = spread(backup_count.values()), spread(main_count.values())
        st = spread(totals.values())
        larger = model.NewIntVar(0, 2 * H, "")
        model.Add(larger >= sb)
        model.Add(larger >= sm)
        overage = scheduler._historic_overage()
        least = model.NewIntVar(0, 2 * H, "")
        excess = []
        for n in nurses:
            model.Add(least <= totals[n])
            e = model.NewIntVar(0, 4 * H + 1000, "")
            model.Add(e >= int(overage.get(n, 0)) + totals[n] - least)
            excess.append(e)
        gaps = len(self.slots) - sum(
            self.x[d, r, n] for d, r in self.slots for n in nurses if (d, r, n) in self.x
        )
        prior = scheduler._prior_violation_counts()
        rot = sum(v for v, _ in self.violations) if self.violations else 0
        rot_viol = (
            sum(v * (1 + int(prior.get(n, 0))) for v, n in self.violations)
            if self.violations
            else 0
        )

        # Same-weekday repeats: sum over nurse and weekday of C(count, 2).
        repeats = []
        for n in nurses:
            for wd in range(4):
                days = [d for d in weekdays if d.weekday() == wd]
                pins = sum(
                    1 for d in days for r in ROLES if (d, r) not in slot_set and pinned(d, r) == n
                )
                cells = [self.x_day[d, n] for d in days if (d, n) in self.x_day]
                if not cells and pins < 2:
                    continue
                c = model.NewIntVar(0, len(days) * 2, "")
                model.Add(c == pins + sum(cells))
                sq = model.NewIntVar(0, 4 * len(days) ** 2, "")
                model.AddMultiplicationEquality(sq, [c, c])
                repeats.append(sq - c)  # 2 * C(c, 2)

        self.keys = {
            "rotation": rot,
            "gaps": gaps,
            "rot_viol": rot_viol,
            "weekend_gap": self._weekend_gap_penalty(start),
            "balance": sb + sm,
            "larger": larger,
            "long_term": sum(excess),
            "total": st,
            "exceptions": sum(self.exceptions) if self.exceptions else 0,
            "repeats": sum(repeats) if repeats else 0,
        }

    def _weekend_gap_penalty(self, start):
        """NurseScheduler._weekend_gap_penalty, as a linear expression.

        Each nurse's worked weekends in the month form one of a few subsets;
        each subset's gaps are constants, so the penalty (spread of all gaps
        plus their deficit below the target) is linear in subset choices.
        """
        s, model = self.s, self.m
        target = int(s.config.weekend_gap_days)
        gap_min = target
        horizon_base = s.end_date + timedelta(days=target)
        horizon = horizon_base + timedelta(days=(s.FRIDAY_WEEKDAY - horizon_base.weekday()) % 7)
        base = self.root.state.schedule
        generated = set(self.fridays)
        # Fridays worked in the window outside the generated weekends (pins).
        fixed_fridays = {}
        for day in base.index:
            if day.weekday() == s.FRIDAY_WEEKDAY and day not in generated:
                for r in ROLES:
                    v = base.at[day, r]
                    if not is_empty(v):
                        fixed_fridays.setdefault(v, set()).add(s._as_friday(day))

        def gaps_of(nurse, scheduled):
            history = sorted(
                {
                    d
                    for d in (
                        s._as_friday(pd.Timestamp(h).normalize())
                        for h in s.weekend_history.get_weekends(nurse)
                    )
                    if d is not None and d < start
                }
            )
            if not history:
                history = [s._as_friday(start - timedelta(days=target + 1))]
            timeline = sorted(set(history) | set(scheduled))
            if timeline[-1] < horizon:
                timeline.append(horizon)
            out = []
            for prev, curr in itertools.pairwise(timeline):
                if curr < start:
                    continue
                g = (curr - prev).days
                if g > 0:
                    out.append(g)
            if not out and len(timeline) >= 2:
                g = (timeline[-1] - timeline[-2]).days
                if g > 0:
                    out.append(g)
            return out

        everyone = sorted(set(s.nurses) | set(self.nurses) | set(fixed_fridays))
        deficit = 0
        BIG = 10_000
        max_gap = model.NewIntVar(0, BIG, "max_gap")
        min_gap = model.NewIntVar(0, BIG, "min_gap")
        any_gap = False
        for n in everyone:
            fixed = fixed_fridays.get(n, set())
            if n not in self.nurses:
                g = gaps_of(n, fixed)
                if g:
                    any_gap = True
                    model.Add(max_gap >= max(g))
                    model.Add(min_gap <= min(g))
                    deficit += sum(max(0, target - x) for x in g)
                continue
            subsets = self._weekend_subsets(n, gap_min)
            u = {S: model.NewBoolVar("") for S in subsets}
            model.AddExactlyOne(u.values())
            for f in self.fridays:
                model.Add(self.work[f, n] == sum(v for S, v in u.items() if f in S))
            for S, v in u.items():
                g = gaps_of(n, set(S) | fixed)
                if not g:
                    continue
                any_gap = True
                model.Add(max_gap >= max(g)).OnlyEnforceIf(v)
                model.Add(min_gap <= min(g)).OnlyEnforceIf(v)
                deficit += v * sum(max(0, target - x) for x in g)
        if not any_gap:
            return 0
        return max_gap - min_gap + deficit

    def _weekend_subsets(self, nurse, gap_min) -> list[tuple]:
        """Every set of the month's weekends *nurse* could work, by the gap.

        Two pinned weekends are exempt from the gap, as the generator does.
        """
        subsets: list[tuple] = []

        def extend(chosen, i):
            subsets.append(tuple(chosen))
            for j in range(i, len(self.fridays)):
                f = self.fridays[j]
                if all(
                    (f - c).days >= gap_min
                    or ((c, nurse) in self.pinned_weekend and (f, nurse) in self.pinned_weekend)
                    for c in chosen
                ):
                    extend([*chosen, f], j + 1)

        extend([], 0)
        return subsets

    def solve(self, order, time_limit_s, workers, log):
        cp = self.cp
        model = self.m
        solver = cp.CpSolver()
        solver.parameters.num_workers = workers
        solver.parameters.random_seed = 0
        # Same months every run: parallel workers otherwise race, and equally
        # good months come out in a different order each time.
        solver.parameters.interleave_search = True
        solver.parameters.log_search_progress = log
        began = time.perf_counter()
        deadline = began + time_limit_s
        values, status_names, optimal = {}, [], True
        for key in order:
            expr = self.keys[key]
            if isinstance(expr, int):
                values[key] = expr
                continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                optimal = False
                break
            solver.parameters.max_time_in_seconds = remaining
            model.Minimize(expr)
            status = solver.Solve(model)
            status_names.append((key, solver.StatusName(status), round(solver.WallTime(), 2)))
            if status not in (cp.OPTIMAL, cp.FEASIBLE):
                if not values:
                    return None
                optimal = False
                break
            value = int(round(solver.ObjectiveValue()))
            values[key] = value
            if status != cp.OPTIMAL:
                optimal = False
            model.Add(expr == value)
            model.ClearHints()
            for i in range(len(model.Proto().variables)):
                var = model.GetIntVarFromProtoIndex(i)
                model.AddHint(var, solver.Value(var))
        weekends = {}
        for f in self.fridays:
            a = next(n for n in self.nurses if solver.BooleanValue(self.fsf[f, n]))
            b = next(n for n in self.nurses if solver.BooleanValue(self.sfs[f, n]))
            weekends[f] = (a, b)
        weekdays = {(d, r): n for (d, r, n), v in self.x.items() if solver.BooleanValue(v)}
        return MonthSolution(
            weekends, weekdays, values, optimal, time.perf_counter() - began, status_names
        )


# ── replay through the scheduler's own checks ─────────────────────────────
def replay(scheduler, solution: MonthSolution):
    """Place *solution* with the scheduler's checks.

    Returns the variant, its measures (the ``MONTH_ORDER`` keys) and the
    per-nurse counts. Raises ValueError when a weekend pair is not one the
    generator allows at that point, or a weekday shift fails the
    gap-filling rules.
    """
    from scheduler.scoring import weekday_repeats

    root = root_variant(scheduler)
    pre = scheduler._get_pre_scheduled_weekend_assignments()
    v = root.clone()
    for f in scheduler._get_weekends():
        fsf, sfs = solution.weekends[f]
        pairs = scheduler._get_valid_nurse_pairs(
            f,
            v.state.last_pattern,
            pre.get(f, {}),
            v.state.weekend_tracking,
            schedule=v.state.schedule,
            all_pre_scheduled_weekends=pre,
            enforce_rotation=True,
        )
        if (fsf, sfs) not in pairs:
            raise ValueError(f"weekend {f.date()}: ({fsf}, {sfs}) is not allowed there")
        v.assign_weekend(f, fsf, sfs)
    v.compute_unfillable_slots()
    for (d, r), n in sorted(solution.weekdays.items()):
        if n not in v.get_eligible_nurses_for_day_gap(d, r, log=False):
            raise ValueError(f"{d.date()} {r}: {n} is not allowed there")
        v.place_assignment(d, r, n)
    v.recalculate_assignment_counts()
    sb, sm, st = v.spread_components()
    counts = {
        n: {
            "main": int(v.state.main_assignment_counts[n]),
            "backup": int(v.state.backup_assignment_counts[n]),
            "total": int(v.state.main_assignment_counts[n] + v.state.backup_assignment_counts[n]),
        }
        for n in v.nurses
    }
    df = v.state.schedule
    gaps = sum(
        1
        for d in v.get_weekdays()
        for r in ROLES
        if not v.is_pre_scheduled(d, r) and is_empty(v.state.grid().get(d, r))
    )
    exceptions = sum(v.is_gap_rule_exception(n, d, r) for (d, r), n in solution.weekdays.items())
    return (
        v,
        {
            "rotation": len(v.rotation_violations),
            "gaps": gaps,
            "rot_viol": scheduler._rotation_violation_score(
                df, scheduler._prior_violation_counts()
            ),
            "weekend_gap": scheduler._weekend_gap_penalty(df),
            "balance": sb + sm,
            "larger": max(sb, sm),
            "long_term": scheduler._long_term_score(counts, scheduler._historic_overage()),
            "total": st,
            "exceptions": exceptions,
            "repeats": 2 * weekday_repeats(df),
        },
        counts,
    )


__all__ = ["MONTH_ORDER", "WEEKDAY_ORDER", "MonthSolution", "replay", "solve_month", "unsupported"]
