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
import threading
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
    "relaxed",
    "rot_viol",
    "weekend_gap",
    "balance",
    "larger",
    "long_term",
    "total",
    "exceptions",
    "repeats",
)
# Tie-breakers: each gets at most TIE_BREAKER_S; if not proven by then, the
# best found is kept (the month's other measures are still proven).
TIE_BREAKERS = frozenset({"exceptions", "repeats"})
TIE_BREAKER_S = 10.0
# The exact weekday solve's order, for comparing with it.
WEEKDAY_ORDER = ("gaps", "relaxed", "balance", "larger", "total", "exceptions", "long_term")


@dataclass
class MonthSolution:
    weekends: dict  # friday -> (fsf, sfs)
    weekdays: dict  # (day, role) -> nurse, the modifiable weekday cells only
    values: dict  # key -> value, for the keys proven optimal (replay() gives all)
    optimal: bool  # every key but the tie-breakers proven optimal
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
    try:
        import ortools.sat.python.cp_model  # noqa: F401
    except ImportError:
        return "OR-Tools is not installed"
    return None


def solve_month(
    scheduler,
    *,
    top_n: int = 1,
    order=MONTH_ORDER,
    fixed_weekends: dict | None = None,
    allow_rotation_violations: bool = False,
    time_limit_s: float = 600.0,
    workers: int = 8,
    log: bool = False,
    is_cancelled=None,
    on_month=None,
) -> list[MonthSolution]:
    """The best ``top_n`` months, each with different weekends.

    With ``allow_rotation_violations`` a nurse the scheduler allows to
    repeat (``nurses_allowed_rotation_violation``, or anyone when that is
    empty) may work the same pattern twice in a row; repeats are counted
    and minimized first. ``is_cancelled()`` is polled while solving;
    ``on_month(number)`` is called as each month is found. The result is
    shorter than ``top_n`` when fewer months exist, or time or a
    cancellation ran out: see :func:`solve_month_status` for which.
    """
    return solve_month_status(
        scheduler,
        top_n=top_n,
        order=order,
        fixed_weekends=fixed_weekends,
        allow_rotation_violations=allow_rotation_violations,
        time_limit_s=time_limit_s,
        workers=workers,
        log=log,
        is_cancelled=is_cancelled,
        on_month=on_month,
    )[1]


def solve_month_status(
    scheduler,
    *,
    top_n: int = 1,
    order=MONTH_ORDER,
    fixed_weekends: dict | None = None,
    allow_rotation_violations: bool = False,
    time_limit_s: float = 600.0,
    workers: int = 8,
    log: bool = False,
    is_cancelled=None,
    on_month=None,
) -> tuple[str, list[MonthSolution]]:
    """:func:`solve_month`, and how it ended.

    The status is ``"ok"`` (``top_n`` months, or every month there is),
    ``"infeasible"`` (no month satisfies the rules: proven),
    ``"unsupported"``, ``"cancelled"``, or ``"unknown"`` (time ran out
    before the first month was found).
    """
    if unsupported(scheduler):
        return "unsupported", []
    solutions: list[MonthSolution] = []
    excluded: list[dict] = []
    for _ in range(top_n):
        built = _Model(scheduler, fixed_weekends, excluded, allow_rotation_violations)
        found = built.solve(order, time_limit_s, workers, log, is_cancelled)
        if isinstance(found, str):
            if solutions and found == "infeasible":
                return "ok", solutions  # every month there is
            return (found if not solutions or found == "cancelled" else "ok"), solutions
        solutions.append(found)
        excluded.append(found.weekends)
        if on_month is not None:
            on_month(len(solutions))
    return "ok", solutions


class _Model:
    def __init__(self, scheduler, fixed_weekends, excluded, allow_rotation_violations=False):
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
        # A weekend pattern pinned to a nurse the month does not schedule (a
        # PRN nurse): (friday, pattern) -> nurse. Weekend generation keeps
        # such a pin as it is; the nurse counts toward nothing else.
        self.external = {}
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
            for pattern, table in ((FSF, self.fsf), (SFS, self.sfs)):
                if forced.get(pattern) is not None and forced[pattern] not in nurses:
                    self.external[f, pattern] = forced[pattern]  # every var is 0
                else:
                    model.AddExactlyOne(table[f, n] for n in nurses)
            for n in nurses:
                model.Add(self.fsf[f, n] + self.sfs[f, n] <= 1)
            both_pinned = FSF in forced and SFS in forced
            if not both_pinned:
                for a, b in itertools.permutations(late, 2):
                    if a in nurses and b in nurses:
                        model.Add(self.fsf[f, a] + self.sfs[f, b] <= 1)
                # A late-shift PRN pinned into one pattern: no late partner.
                for pattern, partner in ((FSF, self.sfs), (SFS, self.fsf)):
                    outsider = self.external.get((f, pattern))
                    if outsider and scheduler.nurse_manager.is_late_shift_nurse(outsider):
                        for b in late:
                            if b in nurses:
                                model.Add(partner[f, b] == 0)
        self.work = {(f, n): self.fsf[f, n] + self.sfs[f, n] for f in self.fridays for n in nurses}

        # Weekend gap between the month's own weekends.
        gap_min = cfg.weekend_gap_days
        for f1, f2 in itertools.combinations(self.fridays, 2):
            if (f2 - f1).days < gap_min:
                for n in nurses:
                    if (f1, n) in self.pinned_weekend and (f2, n) in self.pinned_weekend:
                        continue
                    model.Add(self.work[f1, n] + self.work[f2, n] <= 1)

        # Strict alternation. A pinned weekend may repeat, and with repeats
        # allowed so may the nurses allowed to (anyone, when none are
        # listed): those repeats are counted instead.
        last = dict(root.state.last_pattern)
        self.violations = []  # (indicator, nurse)
        may_repeat = set()
        if allow_rotation_violations:
            may_repeat = set(scheduler.nurses_allowed_rotation_violation) or set(nurses)
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
                    if self.pinned_weekend.get((f2, n)) == pattern or n in may_repeat:
                        v = model.NewBoolVar("")
                        for e in exprs:
                            model.Add(v >= e + table[f2, n] - 1)
                        self.violations.append((v, n))
                    else:
                        for e in exprs:
                            model.Add(e + table[f2, n] <= 1)

        if fixed_weekends:
            for f, (a, b) in fixed_weekends.items():
                if (f, a) in self.fsf:
                    model.Add(self.fsf[f, a] == 1)
                if (f, b) in self.sfs:
                    model.Add(self.sfs[f, b] == 1)
        for other in excluded:
            chosen = [self.fsf[f, a] for f, (a, b) in other.items() if (f, a) in self.fsf]
            chosen += [self.sfs[f, b] for f, (a, b) in other.items() if (f, b) in self.sfs]
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

        # A nurse's neighbouring weekends are the month's weekends they work
        # plus fixed ones (history, recorded, pinned cuts).
        static_fridays = {
            n: set(root.state.nurse_weekend_lists.get(n, [])) - set(self.fridays) for n in nurses
        }

        def neighbour(friday, n):
            if friday in static_fridays[n]:
                return 1
            if friday in self.fridays:
                return self.work[friday, n]
            return 0

        def around(d, n):
            """(weekend before, weekend after) *d* that *n* works: 0/1 or expressions."""
            weekday = d.weekday()
            return (
                neighbour(d - timedelta(days=weekday + 3), n),
                neighbour(d + timedelta(days=4 - weekday), n),
            )

        # Spacing, against every shift within reach (and just outside the
        # window). With the one-day gap on, shifts at the outer distance
        # (Mon–Wed, Tue–Thu at 2-day spacing) are a relaxation: allowed only
        # on days away from the nurse's own weekends
        # (_weekday_relaxation_applicable), both days when both are placed
        # here (the exact solve re-checks every placed shift), for the role
        # pairs the settings allow, and counted.
        spacing = int(cfg.min_days_between_assignments)
        relax = bool(cfg.one_day_gap_enabled)
        hard = max(1, spacing - 1) if relax else spacing
        master = bool(cfg.allow_one_day_weekday_gap)
        self.relaxations = []

        def use_relaxation(days_and_nurse):
            r = model.NewBoolVar("")
            for day, nurse in days_and_nurse:
                before, after = around(day, nurse)
                for side in (before, after):
                    if isinstance(side, int):
                        if side:
                            model.Add(r == 0)
                    else:
                        model.Add(r + side <= 1)
            self.relaxations.append(r)
            return r

        def role_of(day, n, role):
            return self.x.get((day, role, n))

        for (d, n), xd in self.x_day.items():
            outside = root._worked_outside_window(n)
            for gap in range(1, spacing + 1):
                relaxed_band = gap > hard
                for other in (d - timedelta(days=gap), d + timedelta(days=gap)):
                    if other not in index:
                        if other in outside:
                            if relaxed_band and master:
                                model.Add(xd <= use_relaxation([(d, n)]))
                            else:
                                model.Add(xd == 0)
                        continue
                    if (other, n) in self.x_day:
                        if other < d:
                            continue  # once per weekday pair
                        xo = self.x_day[other, n]
                        if not relaxed_band:
                            model.Add(xd + xo <= 1)
                            continue
                        r = use_relaxation([(d, n), (other, n)])
                        model.Add(xd + xo <= 1 + r)
                        if not master:
                            for ra, rb in itertools.product(ROLES, ROLES):
                                if not cfg.one_day_gap_allows_roles(ra, rb):
                                    a, b = role_of(d, n, ra), role_of(other, n, rb)
                                    if a is not None and b is not None:
                                        model.Add(a + b <= 1)
                        continue
                    w = works(other, n)
                    if not relaxed_band:
                        if isinstance(w, int):
                            if w:
                                model.Add(xd == 0)
                        else:
                            model.Add(xd + w <= 1)
                        continue
                    if isinstance(w, int) and not w:
                        continue
                    r = use_relaxation([(d, n)])
                    if isinstance(w, int):
                        model.Add(xd <= r)
                    else:
                        model.Add(xd + w <= 1 + r)
                    if not master and other not in generated_days:
                        # A pinned shift: its role is known.
                        for rb in ROLES:
                            if (other, rb) not in slot_set and pinned(other, rb) == n:
                                for ra in ROLES:
                                    a = role_of(d, n, ra)
                                    if a is not None and not cfg.one_day_gap_allows_roles(ra, rb):
                                        model.Add(a == 0)

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

        # Weekend windows.
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

            after = neighbour(prev_f, n)
            if not allowed_after:
                if isinstance(after, int):
                    if after:
                        model.Add(var == 0)
                else:
                    model.Add(var + after <= 1)
            ahead = neighbour(next_f, n)
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
        # Pinned outsiders' repeats are fixed: follow each one's own pattern.
        outsider_last = dict(root.state.last_pattern)
        for f in self.fridays:
            for pattern in (FSF, SFS):
                outsider = self.external.get((f, pattern))
                if outsider is not None:
                    if outsider_last.get(outsider) == pattern:
                        rot += 1
                        rot_viol += 1 + int(prior.get(outsider, 0))
                    outsider_last[outsider] = pattern

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
            "relaxed": sum(self.relaxations) if self.relaxations else 0,
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

        for (f, _pattern), outsider in self.external.items():
            fixed_fridays.setdefault(outsider, set()).add(f)
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

    def solve(self, order, time_limit_s, workers, log, is_cancelled=None):
        """A :class:`MonthSolution`, or "infeasible", "unknown" or "cancelled"."""
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
        stop = threading.Event()
        cancelled = [False]

        def watch():
            while not stop.wait(0.2):
                if is_cancelled():
                    cancelled[0] = True
                    solver.StopSearch()
                    return

        watcher = None
        if is_cancelled is not None:
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
        exprs = {k: v for k, v in self.keys.items() if not isinstance(v, int)}
        snapshot = None  # the cells of the last solution found
        proven: dict = {}  # keys minimized to proven optimality, and their values
        status_names, optimal = [], True
        try:
            for key in order:
                expr = self.keys[key]
                if isinstance(expr, int):
                    continue
                remaining = deadline - time.perf_counter()
                if remaining <= 0 or cancelled[0]:
                    optimal = False
                    break
                tie_breaker = key in TIE_BREAKERS
                solver.parameters.max_time_in_seconds = (
                    min(remaining, TIE_BREAKER_S) if tie_breaker else remaining
                )
                model.Minimize(expr)
                status = solver.Solve(model)
                status_names.append((key, solver.StatusName(status), round(solver.WallTime(), 2)))
                if cancelled[0]:
                    return "cancelled"
                if status == cp.INFEASIBLE and snapshot is None:
                    return "infeasible"
                if status not in (cp.OPTIMAL, cp.FEASIBLE):
                    if tie_breaker and snapshot is not None:
                        continue  # keep the month found so far
                    optimal = False
                    break
                snapshot = self._snapshot(solver, exprs)
                if status != cp.OPTIMAL:
                    if tie_breaker:
                        # Keep this one's best, and go on to the next.
                        model.Add(expr <= int(round(solver.ObjectiveValue())))
                        self._hint(solver)
                        continue
                    optimal = False
                    break
                value = snapshot[0][key]
                proven[key] = value
                model.Add(expr == value)
                self._hint(solver)
        finally:
            stop.set()
        if snapshot is None:
            return "cancelled" if cancelled[0] else "unknown"
        _values, weekends, weekdays = snapshot
        # Only keys minimized to optimality have tight values (a spread's
        # high and low marks are free otherwise); replay() gives them all.
        values = {k: v for k, v in self.keys.items() if isinstance(v, int)}
        values.update(proven)
        values = {k: values[k] for k in self.keys if k in values}
        return MonthSolution(
            weekends, weekdays, values, optimal, time.perf_counter() - began, status_names
        )

    def _hint(self, solver):
        """Start the next solve from the solution just found."""
        model = self.m
        model.ClearHints()
        for i in range(len(model.Proto().variables)):
            var = model.GetIntVarFromProtoIndex(i)
            model.AddHint(var, solver.Value(var))

    def _snapshot(self, solver, exprs):
        values = {k: int(round(solver.Value(e))) for k, e in exprs.items()}
        weekends = {}
        for f in self.fridays:
            pair = []
            for pattern, table in ((FSF, self.fsf), (SFS, self.sfs)):
                outsider = self.external.get((f, pattern))
                pair.append(
                    outsider
                    if outsider is not None
                    else next(n for n in self.nurses if solver.BooleanValue(table[f, n]))
                )
            weekends[f] = tuple(pair)
        weekdays = {(d, r): n for (d, r, n), v in self.x.items() if solver.BooleanValue(v)}
        return values, weekends, weekdays


# ── replay through the scheduler's own checks ─────────────────────────────
def replay(scheduler, solution: MonthSolution, *, allow_rotation_violations: bool = False):
    """Place *solution* with the scheduler's checks.

    Each weekend pair must be one weekend generation allows at that point
    (with repeats only for nurses allowed to, when *allow_rotation_violations*),
    and each weekday shift must pass the gap-filling rules with every other
    shift in place (with the one-day gap when it is on), as the exact weekday
    solve re-checks its fills.

    Returns the variant, its measures (the ``MONTH_ORDER`` keys) and the
    per-nurse counts. Raises ValueError when a rule is broken.
    """
    from scheduler.scoring import weekday_repeats

    root = root_variant(scheduler)
    pre = scheduler._get_pre_scheduled_weekend_assignments()
    v = root.clone()
    for f in scheduler._get_weekends():
        fsf, sfs = solution.weekends[f]
        common = dict(schedule=v.state.schedule, all_pre_scheduled_weekends=pre)
        args = (f, v.state.last_pattern, pre.get(f, {}), v.state.weekend_tracking)
        allowed = scheduler._get_valid_nurse_pairs(*args, enforce_rotation=True, **common)
        if (fsf, sfs) not in allowed and allow_rotation_violations:
            allowed = scheduler._get_valid_nurse_pairs(
                *args,
                enforce_rotation=False,
                nurses_allowed_rotation_violation=scheduler.nurses_allowed_rotation_violation,
                **common,
            )
        if (fsf, sfs) not in allowed:
            raise ValueError(f"weekend {f.date()}: ({fsf}, {sfs}) is not allowed there")
        v.assign_weekend(f, fsf, sfs)
    v.compute_unfillable_slots()
    for (d, r), n in sorted(solution.weekdays.items()):
        v.place_assignment(d, r, n)
    relax = bool(scheduler.config.one_day_gap_enabled)
    grid = v.state.grid()
    for (d, r), n in sorted(solution.weekdays.items()):
        grid.set(d, r, None)
        ok = n in v.get_eligible_nurses_for_day_gap(d, r, relaxed_spacing=relax, log=False)
        grid.set(d, r, n)
        if not ok:
            raise ValueError(f"{d.date()} {r}: {n} is not allowed there")
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
            "relaxed": _relaxations(v, solution) if relax else 0,
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


def _relaxations(v, solution) -> int:
    """Pairs of a nurse's shifts only the one-day gap allows, one per pair.

    At least one shift of each pair is a weekday shift the month placed.
    """
    spacing = int(v.config.min_days_between_assignments)
    hard = max(1, spacing - 1)
    placed = {(d, n) for (d, _r), n in solution.weekdays.items()}
    count = 0
    for d, n in placed:
        outside = v._worked_outside_window(n)
        for gap in range(hard + 1, spacing + 1):
            for other in (d - timedelta(days=gap), d + timedelta(days=gap)):
                if (other, n) in placed:
                    count += other > d  # count a placed pair once
                elif other in v.state.positions():
                    count += v._nurse_assigned_on_date(n, other)
                else:
                    count += other in outside
    return count


__all__ = [
    "MONTH_ORDER",
    "WEEKDAY_ORDER",
    "MonthSolution",
    "replay",
    "solve_month",
    "solve_month_status",
    "unsupported",
]
