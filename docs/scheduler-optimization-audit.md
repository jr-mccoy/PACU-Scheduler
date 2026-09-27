# Scheduler optimization audit

A review of where the scheduling engine spends its time, and of how to make
it faster **without reducing or pruning the search space**, so that no
higher-quality schedule becomes harder to find. Line references are to
commit `1b93337`, the state before step 1 of [the plan](#plan), unless a
finding says otherwise.

Each finding says what it costs, how it was measured, and the change it
needs. There are two kinds:

- **Lossless speedups** change how fast the scheduler runs but not a single
  cell of what it produces.
- **Search fixes** change what it produces, and only for the better: each
  one widens the search or stops it throwing away a better schedule it has
  already found.

Nothing here removes candidates, lowers a cap, or adds an early exit that
could hide a better schedule. [Excluded on purpose](#excluded-on-purpose)
lists the ideas that would.

## Method

- Profiled weekend generation and one variant's evaluation with `cProfile`
  on the demo roster (`scripts/demo.py`: 8 regular nurses, 4 weeks, default
  `WorkerTuningConfig`, beam cap 1,000), on a 4-core container.
- Timed evaluation without the profiler, one variant per process.
- Prototyped the cheapest fixes by monkeypatching, and compared SHA-1
  fingerprints of the schedules produced before and after.
- Enumerated every legal fill of each week to measure how much of the week
  neighbourhood the rebalance actually reaches.
- Solved the weekday balance problem exactly with OR-Tools CP-SAT, to see
  how far the current search is from the optimum. OR-Tools was used only for
  this measurement; it is not a dependency.

## Summary

| # | Kind | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | Speedup | Weekday tie-breaker counts are rebuilt through pandas on every domain | Measured: 45% of rebalance | Fixed |
| 2 | Speedup | Each slot's first domain member is re-checked by `_inc_assign` | Measured: ~10% | Fixed |
| 3 | Speedup | The hot path reads and writes a pandas DataFrame cell by cell | Measured: `.at` is 45% after 1–2 | Fixed |
| 4 | Speedup | Facts fixed once the weekends are fixed are recomputed on every check | Code reading | Fixed |
| 5 | Speedup | Weekend generation repeats branch-independent work and clones every child | Measured: 6.5 s for 160 variants | Fixed |
| 6 | Speedup | Smaller redundancies: count recalculations, history scans in ranking | Measured: ~1.5 s per 1,000 candidates | Won't fix |
| 7 | Search | Hard-coded (1, 1) spread targets stop the search before it is done | Measured | Fixed |
| 8 | Search | The full-period refill discards improvements that miss the target | Code reading, test | Fixed |
| 9 | Search | The rebalance reaches a small, mostly failing slice of each week | Measured | Superseded by 12 |
| 10 | Search | Neutral (plateau) moves never happen in the rebalance | Code reading, measured | Superseded by 12 |
| 11 | Search | The window refill keeps the first completion it finds, not the best | Code reading | Superseded by 12 |
| 12 | Search | Weeks are independent; the weekday problem can be solved exactly | Measured | Fixed |
| 13 | Search | Spend the time saved on the caps that do cut the search | Measured | Done; beam cap kept |

## Where the time goes

One weekend variant, profiled (65.7 s under `cProfile`):

| Phase | Share |
|---|---|
| Rebalance (`iterative_rebalance_no_revert`) | **98%** |
| – `_weekday_counts_for` | 45% of rebalance |
| – pandas `.at` cell access (931k calls), mostly spacing and weekly-limit checks | ~26% |
| Gap-fill, window refill, clone | ~2% |

Without the profiler a variant took 20–28 s serially, and a run evaluates
every surviving variant (160 on the demo). Weekend generation for all 160
variants took 6.5 s, about 60% of it pandas availability lookups.

## Lossless speedups

### 1. Weekday tie-breaker counts are rebuilt through pandas on every domain

**Where.** `scheduler/domain.py:1848-1871` (`_weekday_counts_for`), cleared by
`_invalidate_weekday_cache` from `_inc_assign` (`:1560`) and `_dec_assign`
(`:1585`).

**What happens.** Every candidate domain is sorted by how often each nurse
already works that weekday. The count is cached, but every assignment and
unassignment clears the cache, so it is rebuilt thousands of times per
variant, each time by boolean-filtering the frame and calling
`value_counts` twice.

**Evidence.** 29.4 s of a 64.6 s rebalance (45%).

**Fix.** Count directly over the weekday's row positions, computed once.
Skip only missing values, as `value_counts` does.

**Status: Fixed.** `_weekday_counts_for` counts over the positions from
`_weekday_row_positions()`. With finding 2, evaluation is 2.0–2.4× faster and
produces byte-identical schedules on 9 variants across three scenarios
(demo, a day nobody can work, one-day-gap relaxation). Tests:
`tests/test_search_hot_path.py` checks the counts against `value_counts`
at every call of a whole evaluation, and on missing cells and weekend rows.

### 2. Each slot's first domain member is re-checked by `_inc_assign`

**Where.** `scheduler/domain.py:1991-2004` (`_assign_slot_sequence`).

**What happens.** The slot's domain is built from the current state by the
same rules `_inc_assign` checks: the gap-fill rules in gap mode, with the
same one-day-gap fallback. The domain is in fact stricter, since it also
rules out the nurse working the other role that day. So its first member
always passes, and the second check is pure cost.

**Evidence.** About 10% of evaluation. Placing `domain[0]` directly left
every schedule identical.

**Fix.** Place the first domain member without re-checking.

**Status: Fixed.** `_assign_slot_sequence` calls the new
`_place_assignment`, the unchecked half of `_inc_assign`. The check moved to
`_can_inc_assign`, so a test can assert, over whole evaluations under the
default, one-day-gap and midweek-pair settings, that every nurse placed
unchecked is one the full check accepts. `_backtrack_full_order` and
`WindowRefillOptimizer.backtrack_window` make the same redundant check and
can take the same change later. Tests: `tests/test_search_hot_path.py`.

### 3. The hot path reads and writes a DataFrame cell by cell

**Where.** `_has_sufficient_spacing` (`domain.py:1649`),
`_nurse_assigned_on_date` (`:1714`), `_validate_weekly_assignment_limits`
(`:1719`), `_restore_from_backup` (`:2772`), `backup_week_assignments`
(`:2129`), `_get_total_counts` (`:1834`).

**What happens.** Every eligibility check reads single cells through
`DataFrame.at`, which costs microseconds per read in pandas. Backups copy
`.loc` slices and whole count Series, and the total counts are rebuilt as a
Series after every assignment.

**Evidence.** After findings 1 and 2, `.at` is still 45% of evaluation time
(785k reads), spacing 34%, weekly limits 18%, backup restores 15%. These
overlap.

**Fix.** Inside the search, keep the schedule as plain Python structures:
- two lists (main and backup) indexed by day position;
- a bitmask of worked days per nurse, so the spacing check is one AND;
- per-nurse, per-week counters for the weekly limits;
- counts kept as lists or dicts, updated in place.

Build the DataFrame only when a result leaves the worker, and keep the
`VariantSearchContext` interface so the optimizers do not change. Expected
gain: another 3–5×.

**Status: Fixed**, by a simpler route than the bitmasks, because the lists
alone removed the pandas cost:
- **Grid.** `ScheduleState` keeps the Main and Backup cells in a
  `ScheduleGrid` of plain lists, which every search pass reads and writes.
  The `schedule` frame stays the public form. Reading it first writes the
  changed cells back and hands the cells to the frame, so code and tests
  that read or write the frame directly keep working.
- **Neighbours.** Spacing and weekly-limit checks find the neighbouring days
  through cached row positions. These are computed with the same Timestamp
  arithmetic as before, so a date falls inside the schedule exactly when it
  did.
- **Availability.** It is read into plain dicts once per variant (and shared
  with its clones).
- **Backups and counts.** Week backups copy grid cells instead of frame
  slices. The total counts add the two count arrays directly instead of
  aligning two Series. The domain sort reads plain dicts. Recounting
  assignments counts over the grid and writes all counts in one step.

Step 3 (with finding 4) made evaluation 7.6–10× faster than step 2, with
byte-identical schedules on 33 variants across five scenarios (see
[step 3 results](#step-3-results)). Tests: `tests/test_schedule_grid.py`
checks the hand-over between grid and frame, clones and pickles, and the
rewritten spacing and weekly-limit rules. It compares them with the
frame-based versions on random schedules with weekends, a pinned cell and
shifts just before the window.

### 4. Facts fixed once the weekends are fixed are recomputed on every check

**Where.** `_validate_weekday_relative_to_weekend` (`domain.py:1744`) and its
gap variant (`:1778`), which build a new `WeekdayConstraintConfig` on every
call; `_is_in_pre_weekend_window` and `_is_in_post_weekend_window`
(`:1809-1827`); `_weekday_relaxation_applicable` (`:1082`).

**What happens.** Once the weekends are fixed, `nurse_weekend_lists` does not
change for the rest of the evaluation. So each nurse's pre-weekend window,
post-weekend window, relaxation applicability and availability on each day
are constants, yet they are recomputed by bisect and pandas lookups on
every check.

**Fix.** Compute a per-(nurse, day) table once, in
`compute_unfillable_slots` or next to it, and look it up.

**Status: Fixed**, as caches rather than a precomputed table.
- Each nurse's neighbouring Fridays, which decide the pre- and post-weekend
  windows and whether the relaxation applies, are cached per (nurse, day).
- The cache is cleared by everything that edits the Friday lists:
  `assign_weekend`, the pre-scheduled seeding, and snapshot restores.
- The `WeekdayConstraintConfig` is built once and rebuilt only when its
  settings change.

### 5. Weekend generation repeats branch-independent work and clones every child

**Where.** `scheduler/engine.py:1148-1153` (availability),
`:918-932` and `:852-858` (gap checks), `scheduler/repositories.py:1004`
(`get_weekends`), `engine.py:1377-1383` (`_branch`).

**What happens.**
- Availability for a whole weekend doesn't depend on the branch, yet it is
  read through `.loc` plus `.apply(lambda)` for every nurse, on every
  branch, at every weekend.
- The backward gap check scans the branch's schedule for earlier Fridays,
  the forward check boolean-masks it, and `get_last_weekend_before` walks
  every recorded weekend. The branch already keeps sorted per-nurse Friday
  lists, which a bisect can search.
- Every surviving child is a full `ScheduleVariant` clone, whose
  construction and `assign_weekend` each recalculate every count from the
  frame.

**Fix.**
- Precompute availability per (weekend, nurse).
- Answer the gap checks by bisect over presorted lists, with a differential
  test that the answers match.
- Carry branches as light records (last patterns, Friday lists, violations,
  and the pairs chosen) and build a `ScheduleVariant` only for the final
  survivors.

This matters most once the beam cap is raised (finding 13).

**Status: Fixed**, without the light branch records, which turned out not
to be needed. A profile of the March roster (10 nurses) put 80% of the time
in the per-nurse checks: half in reading availability through `.loc`, a
third in the gap checks scanning the branch's schedule.
- **Per-run cache.** A nurse's availability for a weekend, their last
  weekend in history, and their next pre-scheduled or recorded weekend
  depend only on the nurse and the weekend, so one generation run caches
  them (`NurseScheduler._weekend_generation_cache`, cleared when the run
  ends).
- **One pass per branch.** Each branch's schedule is read once per weekend
  (`_weekend_occupancy`); the backward and forward gap checks are answered
  from that (`_check_weekend_gap_from_occupancy`).
- **Pair filter.** The weekend's prefilled cells are read once, not once per
  pair.
- **Clones.** Seeding the pre-scheduled cells, which every clone repeats,
  works on the grid instead of cell by cell on the frame.

Called directly, `_check_weekend_gap_constraints` still takes the original
path. Every variant is identical (SHA-1 over schedules, rotation repeats
and weekend lists, on four rosters), and so is every answer.
`tests/test_weekend_generation_speed.py` checks the variants against the
uncached path on eight scenarios, strict and relaxed, and the gap check on
random schedules, including a window starting on a cut weekend. The random
check catches mutations that the generation scenarios do not.

| Roster | Variants | Before | After |
|---|---|---|---|
| March, 10 nurses (Susan PRN) | 1,000 | 34 s | 5.7 s |
| March, 8 nurses | 856 | 25 s | 4.8 s |
| Demo | 160 | 2.6 s | 0.6 s |

(Timed with a four-worker run sharing the machine; the ratios hold.)

### 6. Smaller redundancies

- `_assign_slot_sequence` recalculates every count from the frame after each
  ordering (`domain.py:2007`). The counts are already kept up to date
  incrementally, as the tracker's comments note.
- Ranking in the parent calls `weekend_history.get_weekends(nurse)`, which
  scans all history, for every nurse of every candidate
  (`_weekend_gap_penalty`, `engine.py:542`).
- Each work item pickles read-only data (availability, pre-schedule, nurse
  manager, history). A pool initializer could ship it once per worker. This
  is minor at 8 nurses.

**Status: Won't fix, measured.** With the exact weekday solve the first
item only affects the fallback search. On the March roster (10 nurses) the
whole ranking of 1,000 candidates takes about 1.5 s (weekend gap penalty
1.0 s, same-weekday repeats 0.4 s, rotation score 0.1 s) against about
20 minutes of evaluation, and pickling is similarly small.

## Search fixes

### 7. Hard-coded (1, 1) spread targets stop the search before it is done

**Where.** `scheduler/evaluation/config.py:25`
(`window_refill_target_spread`), `scheduler/evaluation/worker.py:108` (the
full-period refill runs only while a spread is above 1),
`scheduler/domain.py:2548` (week early stop).

**What happens.** These stop once the backup and main spreads are both at
most 1. None of them considers total spread or the long-term history
penalty, which come later in the lexicographic key the tracker optimizes.
Nor is (1, 1) always the best possible: a spread of 0 is possible whenever
a role's slot total divides evenly among the nurses.

**Evidence.** Of 8 sampled demo variants (every 20th of 160), 4 finished at
total spread 2. With the window-refill target set to `None`, 2 of those 4
reached total spread 0, for about 2–3 s more each (finding 12 shows all 4
can).

**Fix.** Replace the fixed targets with a computed lower bound per key (for
example, a role's spread can only be 0 when its slot total divides evenly by
the nurse count), and stop only when every key reaches its bound. Stopping
at a proven bound loses nothing, and it can end the search earlier than now.

**Status: Fixed.** `ScheduleVariant.spread_lower_bounds()` gives proven
lower bounds on the backup, main and total spreads, once no fillable weekday
slot is empty. For a role with `T` shifts over `n` nurses:
- the busiest nurse has at least `ceil(T / n)`, and at least their fixed
  shifts (weekends and pinned cells);
- the least busy nurse has at most `floor(T / n)`, and at most the most they
  could ever be given: their fixed shifts plus, for each week, the weekly
  limits (one Main, two shifts) applied to the open slots on days they are
  available;
- a spread of 0 needs `T` to divide evenly.

The window refill and the full-period refill now stop early only when every
spread reaches its bound, and the worker runs the full-period refill
whenever they have not. That is what `None` now means for
`window_refill_target_spread` and `full_period_target_spread`, which are the
defaults. A `(backup, main)` tuple still gives the old behaviour. The week
early stop was dead code: the statement after it already stopped at the
first improvement. It is removed.

On the 8 sampled demo variants, every one now finishes at (1, 1, 0), which
finding 12 proved optimal. Before, 4 of them finished at (1, 1, 2). Two of
those four got there by the window refill alone. The other two (variants 5
and 7) needed the full-period refill, which the old gate never ran, at
about 6.5 s more each. See [step 2 results](#step-2-results). Tests:
`tests/test_spread_lower_bounds.py` checks the bounds against every legal
fill of small instances, and checks where each pass stops.

### 8. The full-period refill discards improvements that miss the target

**Where.** `scheduler/optimization/window_refill.py:244` (`required_spread`
defaults to `True`), `:314`; `scheduler/evaluation/worker.py:110` (never
overrides it).

**What happens.** The refill records the best complete refill it finds
(`best_rows`), but applies it only when `required_spread` is false. The
worker leaves it true, so any improvement that does not reach (1, 1) is
thrown away. That is exactly the case where the refill runs at all, since
the worker calls it only while a spread is above 1. The tracker already
reverts anything that is not better, so the flag protects nothing.

**Fix.** Have the worker pass `required_spread=False`.

**Status: Fixed.** The worker passes `required_spread=False`. On the demo
roster the refill rarely completes within its budget, so no schedule
sampled there changed. The fix matters where the target cannot be reached,
such as a nurse on long leave. Tests: `tests/test_search_hot_path.py` shows
the refill keeping a better schedule that misses the target, and that the
worker asks it to. Both tests fail without the fix.

### 9. The rebalance reaches a small, mostly failing slice of each week

**Where.** `scheduler/domain.py:2482-2589`
(`_try_week_permutations_no_revert`), `:2525` (`slot_orderings`).

**What happens.** For each week the rebalance clears the week, then refills
it greedily in up to `max_week_permutations` (200) slot orderings, each
nurse chosen first-fit with no backtracking. It keeps the first ordering
that improves the spreads.

**Evidence.** Three variants, instrumented:
- In weeks that did not improve, 75–99% of the orderings dead-ended before
  filling the week.
- The orderings that did complete produced only 1 to 63 distinct fills.
- Enumerating every legal fill showed each week has only 80–3,024 in total.
  Listing them took 0.5–6.6 s even with today's pandas checks.
- In one week the enumeration found (1, 1, 0) from a start of (2, 1, 2), the
  best possible single-week move, where the sampled orderings found no
  improvement.

**Fix.** Search every legal fill of the week, by backtracking that keeps the
best completion (under a node budget as a safety net), and apply the best
rather than the first improvement. Every fill the sampled orderings could
produce is among them, so this can only do better.

**Status: Superseded by finding 12.** The exact weekday solve lists every
legal fill of every week, which is the fix proposed here, taken all the way.
The rebalance now runs only when the exact solve steps aside (OR-Tools
missing, spacing of 4 or more days, or too many fills in a week), so it is
left as it is.

### 10. Neutral (plateau) moves never happen in the rebalance

**Where.** `scheduler/domain.py:2540` (a week change needs a strict spread
improvement), `:2192` (a pass with no change ends the loop).

**What happens.** Because a week change is kept only when it strictly
improves the spreads, every pass that changes something is strictly better,
and a pass that changes nothing ends the loop. `max_plateau_depth` therefore
has no effect in the rebalance: every evaluation records exactly one
"neutral", on its last, unchanged pass. Max-minus-min spread is flat across
many schedules, so the search stalls at local optima.

**Fix.** Add a finer tie-break after the existing keys: the sum of squared
deviations from the mean, or the number of nurses at the maximum and the
minimum. It never trades against a primary key; it only gives the search a
slope across ties. Then allow sideways moves, keeping the best schedule so
far as the tracker already does.

**Status: Superseded by finding 12.** The exact solve has no plateaus to
cross; this affects only the fallback search.

### 11. The window refill keeps the first completion it finds, not the best

**Where.** `scheduler/optimization/window_refill.py:198`, `:211`.

**What happens.** Each window is cleared and refilled by MRV backtracking,
which returns the first complete fill. That fill is kept only if it
improves the spreads. Other completions of the same window, possibly
better, are never looked at.

**Fix.** Keep searching after the first completion, within the node budget,
and keep the best.

**Status: Superseded by finding 12.** The exact solve compares every fill;
this affects only the fallback search, whose refill passes now also have
total budgets.

### 12. Weeks are independent; the weekday problem can be solved exactly

**What happens.** With `min_days_between_assignments` at 3 or less (the
default is 2), every weekday rule that reaches outside a week lands on a
fixed weekend day:
- spacing from Monday back, or from Thursday forward;
- the pre- and post-weekend windows;
- the weekly limits, which stay inside one week.

So which fills are legal in one week doesn't depend on the other weeks. The
weekday problem is "choose one fill per week", coupled only by the counts
that the spreads measure.

**Evidence.**
- For 9 weeks across 3 variants, the set of legal fills was identical with
  the other weeks empty and with them filled.
- CP-SAT, choosing one count pattern per week from each week's legal fills
  (54–358 distinct patterns per week), found the optimum in 0.49–0.55 s per
  variant. Listing the fills took about 7 s with today's pandas checks.

| Variant (of 8 sampled) | Current pipeline (b, m, t) | Exact optimum |
|---|---|---|
| 0, 2, 3, 6 | (1, 1, 0) | (1, 1, 0) |
| 1, 4, 5, 7 | **(1, 1, 2)** | **(1, 1, 0)** |

Removing only the window-refill target left variants 5 and 7 at 2. They
reached 0 once step 2 also let the full-period refill run (finding 7). So
these schedules were found only by the most expensive pass, which the exact
approach would replace.

**Fix.**
- **Without a new dependency.** Keep each week's legal fills and run local
  search over which fill each week uses. A move costs a few integer
  operations instead of a pandas refill, so the whole neighbourhood can be
  scanned.
- **With OR-Tools CP-SAT.** A model at the slot level proves optimality and
  also covers settings where weeks do interact. Later, the weekends could
  join the same model, which would remove the weekend beam entirely.

**Caveats.**
- Check the independence condition at run time, and fall back, or add
  constraints between neighbouring weeks, when spacing is 4 days or more.
- Add gaps (partial fills) and the long-term penalty to the objective.
- Choose among fills with the same count pattern by the existing weekday
  tie-breaks.
- The final ranking normalizes against the candidate pool, which does not
  map directly onto one solver objective; keep that ranking as it is.

**Status: Fixed**, with OR-Tools CP-SAT (`scheduler/optimization/exact_weekdays.py`),
now the default way the weekdays are filled (`WorkerTuningConfig.weekday_solver
= "exact"`).

How it works:
- **Fills.** For each week, every legal fill is listed with the scheduler's
  own eligibility checks, so the rules still have one implementation. Fills
  are grouped by the per-nurse Main and Backup counts they add.
- **Choice.** CP-SAT chooses one fill per week.
- **Objective.** It minimizes, in order: unfilled slots, then main + backup
  spread, then the larger of the two, then total spread, then gap-rule
  exceptions, then the long-term history penalty (`exact_objective =
  "balanced"`, chosen by the owner). `"backup_first"` keeps the search's
  order instead (backup, main, total, history), which on the 8-nurse March
  roster gives lopsided results such as backup spread 0 with main spread 5.
- **Gap-rule exception.** Working the Tuesday right before your own weekend
  is allowed only by the gap-filling rules. By the owner's decision it may be
  used anywhere, but as few times as give the best spreads
  (`exact_gap_rule_exceptions = "for_balance"`). `"when_needed"` allows it
  only in a week that cannot be staffed otherwise.
- **Verification.** The chosen fills are placed through the normal checks,
  so every shift is re-verified; a failure undoes the placement and hands the
  variant to the search.
- **Fallback.** The existing local search runs instead when OR-Tools is not
  installed, when `min_days_between_assignments` is 4 or more (spacing then
  reaches across a weekend), or when a week has more than
  `exact_max_fills_per_week` fills. If CP-SAT runs out of time
  (`exact_time_limit_ms`) it keeps its best schedule and reports it as not
  proven.
- **Reporting.** Each candidate's stats say which filled it (`solver`) and
  whether it is proven optimal (`solver_optimal`).

Making it fast needed three things:
- **Solver settings.** CP-SAT's presolve and probing spent 6–8 s loading this
  model (thousands of fill choices feeding a few per-nurse sums) before
  searching. With both off, each key solves and proves in well under a
  second.
- **Proven lower bounds** (from step 2) as constraints, so a schedule that
  reaches them is proven at once.
- **Enumeration on the grid only.** Trial placements skip the count Series,
  and the exception check is cached per nurse and day.

The GUI ran with the assignment debug logger on by default (see
`ui/settings.py`; now off). Logging every enumeration probe made the solve
run out of time there, so enumeration no longer logs.

Results, sampled variants, default settings:

| Roster (8 variants) | Exact solve | Proven optimal | Against the search |
|---|---|---|---|
| Demo | 2.5–2.7 s | 8/8 | same on 6, better total spread on 2 |
| March, 10 nurses (Susan PRN) | 4.0–6.4 s | 8/8 | same on 8 (the search already reached the optimum) |
| March, 8 nurses | 0.8–1.9 s | 8/8 | better on 8: total spread 3 instead of 4–6 |

Full runs, default settings, 4 workers on a 4-core container (other work
shared the machine during part of them):

| Roster | Variants | Wall time | Exact solve per variant | Proven optimal |
|---|---|---|---|---|
| March, 10 nurses (Susan PRN) | 1,000 | 21 min | 2.1–11.0 s | 1,000/1,000 |
| March, 8 nurses | 856 | 7.8 min | 0.2–19.4 s | 856/856 |

Tests: `tests/test_exact_weekdays.py`. Checks include:
- brute force over every legal schedule of a small instance, under both
  objectives, with and without an unfillable week, on an instance where the
  two objectives have different optima;
- every placed nurse legal, with ordinary-rule refusals only ever the
  gap-rule exception;
- deterministic results;
- each fallback;
- never worse than the search;
- the debug-logger regression.

#### Follow-ups

- **Weekday variety.** Fills with the same count pattern score the same, and
  the solve used to take the first one it listed. It now keeps every fill
  and, once the patterns are chosen, a second CP-SAT solve picks one fill
  per week that minimizes the sum over nurses and Mon–Thu weekdays of the
  squared shift count. That counts the pairs of shifts one nurse works on
  the same weekday, the search's same-weekday tie-break made global. It
  keeps every week's pattern, so no quality key changes, and it takes a few
  milliseconds. On four sampled March variants (10 nurses) it cut the pairs
  from 8–12 to 5–6. It only chooses among fills with the patterns the first
  solve chose; other optimal pattern choices might allow fewer repeats.
  `ExactOutcome.weekday_repeats` reports the count, and tests check it
  against brute force.
- **Ranking ties.** On the March runs the top five candidates all had a
  weighted score of 0.000, so candidate order decided among them. Exact ties
  in the weighted score now go to the smaller total-shift spread, then to
  fewer same-weekday repeats (`scheduler.scoring.TIE_BREAKERS`), before
  candidate order. They never override the weighted score.
- **Fallback budgets.** The local search still runs when the exact solve
  steps aside. Its per-attempt limits (800 s, over hundreds of passes or 54
  orders) never bound it, so each refill pass now also has a total budget
  (`window_refill_total_time_ms`, `full_period_total_time_ms`, 120 s each).
- **GUI debug default.** `assignment_debug_enabled` now defaults to off.

### 13. Spend the time saved on the caps that do cut the search

The search is cut in exactly three places:
- `max_week_permutations` (200 orderings per week);
- the (1, 1) targets (finding 7);
- `max_weekend_variants` (the weekend beam). On the demo the beam does not
  bind (160 variants against a cap of 1,000), but it does on larger rosters
  and longer horizons.

Once the speedups land, remove the fixed targets, make the week search
exhaustive (finding 9 or 12), and raise or remove the beam cap.

**Status: Done where it pays; the beam cap is kept, measured.**
- **Fixed targets:** removed (finding 7).
- **Week search:** exhaustive, by the exact solve (finding 12);
  `max_week_permutations` now only limits the fallback search.
- **Beam cap:** kept at 1,000. Weekend generation is now fast enough to run
  uncapped (finding 5), but every variant still costs an exact weekday
  solve. On the March rosters:

| Roster | Capped (1,000) | Uncapped | Best found |
|---|---|---|---|
| 8 nurses | 856 variants, 6.5 min | 2,352 variants, 22 min | identical |
| 10 nurses (Susan PRN) | 1,000 variants, 25 min | 63,744 variants, about 22 h estimated | not run |

On the 8-nurse roster the uncapped run's best is the same as the capped
run's on every measure:
- spreads: backup + main 3, total 3, the best any of its 1,472
  repeat-free, gap-free variants reach;
- weekend spacing penalty 28;
- rotation score 0;
- long-term score 16.

The raw metrics are compared, because the weighted score is normalized per
run. So on real data the cap cut work, not quality. It stays a per-machine
setting (**Weekend variants to evaluate**); raise it when a run reports that
the beam pruned and there is time to spare.

## Beyond the cap

Asked afterwards: can the runs that produce tens of thousands of weekend
variants be sped up, and can we know whether a better schedule is among the
variants the cap discards?

### How the cap chooses

Weekends are generated one at a time. After each, every (branch, valid
pair) child is scored on weekend-only measures: rotation repeats, then how
evenly recent weekends are spread, then the smallest Friday-to-Friday gap.
The best `max_weekend_variants` (1,000) are kept, ties going to generation
order, and the rest are dropped for good. The key cannot see weekdays:
balance and fairness, which decide the final ranking, need the weekday
solve.

On the March roster (10 nurses, Susan PRN) the 63,744 uncapped variants
all have 0 rotation repeats. Their weekend spacing penalty is 35 for
40,512 of them and 70 for 23,232. The cap kept 1,000 of the 40,512 tied at
the best spacing, so it discarded no better-spaced variant; which 1,000 it
kept among the ties was effectively arbitrary.

### Sharing week fills between variants (option 1)

A variant takes about 4.5 s on that roster: 3–3.5 s in CP-SAT and about
0.6 s listing week fills. Several ways of making CP-SAT itself faster were
measured on 12 variants and none helped:
- search strategies (pseudo-cost, LP-guided, quick restarts: 2× slower);
- linearization level 2, no symmetry detection, feasibility jump;
- table constraints instead of one Boolean per pattern (faster on average
  but sometimes failing to prove optimality within 20 s);
- warm starts from a neighbouring variant's solution (no gain).

The first key's time is mostly propagation through the pairwise clauses of
each week's "exactly one pattern" constraint (about 2.8 million on one
variant), and it varies with search luck: 0.2 s or 1.9 s on similar models.

What can be shared is the listing. A week's legal fills depend only on the
cells within spacing reach of it (±3 days), on who works the weekends
either side of it (the weekend windows reach at most 6 days back and 4
forward), on its unfillable slots, and on inputs fixed for the whole run.
The 63,744 variants have only 3,806 distinct week situations. Each worker
process now keeps the fills of its 64 most recent situations
(`exact_weekdays.week_options`), keyed by exactly those inputs plus a
fingerprint of the run's fixed inputs, so entries never cross runs.
Simulating the engine's order, about 55% of week listings hit the cache.
A full 10-nurse March run (1,000 variants, 4 workers) took 19.5 minutes
with the cache, against 21 and 25 minutes for the two earlier runs, which
shared the machine with other work part of the time: roughly 8–20% faster.
Tests compare every cached listing with a fresh one on four scenarios, and
check that different time off in the same weekends is never shared.

### Checking every weekend variant (option 3)

A single CP-SAT model choosing weekends and weekdays together was the
first idea, but on this roster it would hold about 3,806 situations times
1,836 patterns each, around 7 million choices. The same guarantee comes
more cheaply from bounds, in
`scheduler/optimization/exhaustive.py` (`check_all_weekend_variants`, and
`scripts/check_all_variants.py` for a database):

1. Generate every weekend variant, uncapped. Rotation repeats, the rotation
   score and the weekend spacing penalty are exact before any weekday is
   filled.
2. List each distinct week situation once, as the count patterns of its
   fills.
3. From those, give every variant its exact number of unfilled slots and
   lower bounds on balance and on the long-term fairness penalty. A variant
   is ruled out when, against each of the top options, it ranks lower on
   the rank-first measures or is no better on any weighted measure. That
   holds for any positive weights and any shared normalization.
4. Best first, for each variant still open: CP-SAT finds the least balance
   and the least fairness penalty any legal fill gives (a small solve each)
   where its bounds leave them below a top option, and step 3 is repeated.
5. If it is still open, evaluate it as a run would and rank it with the
   run's candidates. After each round the pool's top options become the
   reference, and the variants still waiting are checked against them
   again, so once better schedules are found, the variants that can at best
   tie them need neither step.

| Roster | Variants | Situations | Ruled out | Can only tie | Evaluated | Better | Time |
|---|---|---|---|---|---|---|---|
| March, 8 nurses | 2,352 | 1,462 | 896 | 1,456 | 0 | 0 | 2 min after the run (full evaluation: 22 min) |
| March, 10 nurses (Susan PRN) | 63,744 | 3,806 | 23,232 | 40,496 | 16 | **5** | 14 min after the run (full evaluation: about 22 h) |

**The cap did discard better schedules on the 10-nurse roster.** The
capped run's top five all have balance 2, total spread 2 and a long-term
fairness penalty of 10: Julia, the nurse with the most days off, gets 6
shifts. Five discarded variants have balance 2, total spread 1 and a
fairness penalty of 1: every nurse gets 7 shifts, and the one working two
weekends gets 8. They are equal on every other measure, so they rank above
the capped five whatever the weights. The check proves nothing ranks above
them: 23,232 variants rank lower and 40,496 can at best tie them. Ties
could still reorder them through the tie-breakers (the new five have 6
same-weekday repeats), which the check does not search.

Bounds alone left 7,296 variants open (their fairness bound was 0 or 1
against 10). Before the best-first loop, solving all of their minima took
over two hours; with it, 16 minima and 16 full evaluations were enough.

Tests (`tests/test_exhaustive_check.py`) compare the bounds with full
evaluations of every variant of three small rosters, check the exact minima
and the ranking logic, and check that the best discarded variant found
matches a full evaluation of every variant.

The tests also found a bug in the exact weekday solve. In a week that
cannot be filled completely, fills can differ in whether they leave a Main
or a Backup slot empty, so each role's total is not fixed. The per-role
spread bounds assumed it was, and could force worse spreads while
reporting them optimal. They are now added only when each role's total is
fixed. Both March rosters fill every slot, so their results were not
affected. A brute-force test over every combination of week patterns
covers it.

### The whole month as one CP-SAT model

Listing weekend variants at all is what forces a cap. The alternative is
one model of the whole month: for each weekend and nurse, whether they work
it as FSF or SFS, and for each weekday slot and nurse, whether they take it
(about 600 yes/no choices for March), with every rule written as a
constraint. CP-SAT then searches every weekend arrangement implicitly and
proves the best month. It is in `scheduler/optimization/month_model.py`
(`solve_month`), with `scripts/solve_month.py` for a database.

It minimizes, in order: rotation repeats, unfilled slots, the rotation
score, the weekend spacing penalty (reproduced exactly), main + backup
spread, the larger of the two, the long-term fairness penalty, total
spread, Tuesday exceptions, and same-weekday repeats. `top_n` asks for
further months with different weekends. Parallel search runs in CP-SAT's
deterministic mode, so a run gives the same months every time.

Because the rules now exist twice, as the scheduler's checks and as
constraints, the model is verified against the checks:
- **Weekends.** The set of weekend arrangements the model allows is exactly
  the set weekend generation produces, on 12 scenarios: history, pinned
  cells, late-shift nurses, five weekends where nurses work two (so
  alternation and the 28-day gap interact), both post-weekend settings,
  spacing of 3 and 4 days, a window starting on a cut weekend, and
  recorded weekends after the window (6 to 1,680 arrangements each).
- **Weekdays.** With a variant's weekends fixed, the model's weekday
  measures equal the exact weekday solve's on every sampled variant where
  that solve proved optimality (over 100 variants).
- **Replay.** `replay` places every solution through the scheduler's own
  checks (each weekend pair must be one the generator allows at that point,
  each weekday shift must pass the gap-filling rules) and recomputes every
  measure; they must equal the model's.

On March:

| Roster | Best month | Top 5 | The run's pipeline |
|---|---|---|---|
| 10 nurses (Susan PRN) | spacing 35, balance 2, fairness 1, total spread 1, 1 same-weekday pair | about 1 min, all proven | 20 min, and missed it (fairness 10) |
| 8 nurses | spacing 28, balance 3, fairness 16, total spread 3 | about 4 min, all proven | 6.5 min, same measures |

Both match what the full-variant check proved best. On the 10-nurse
roster the model also has fewer same-weekday repeats (1 pair against 6),
since it optimizes variety over the whole month.

One side finding: on one small variant the exact weekday solve could not
prove its second key (the larger spread) within its 20 s limit, and so
never minimized the later keys; the month model solved the same weekdays
at once. The March runs proved every variant optimal, so this is rare.

Not modelled yet: the one-day spacing relaxation, allowing rotation
repeats (the fallback when strict alternation has no solution), and a PRN
nurse pinned into a weekend. `month_model.unsupported` says so, and
`solve_month` returns nothing.

## Excluded on purpose

These would save time by pruning, so they are left out:

- Skipping evaluation of variants that cannot reach the top N. For example,
  rotation repeats are known before evaluation and rank first. This would
  not change the winner, but it would remove options from the review list.
- Bound-based early exits across variants.
- Lowering any cap or budget.

## Plan

1. **Findings 1, 2 and 8.** Small and low-risk: 2.0–2.4× faster, identical
   schedules, plus one search fix. *Done.*
2. **Finding 7**, with computed lower bounds. *Done.*
3. **Findings 3 and 4**, behind a differential test that compares schedules
   from the old and new code on the demo, blocked-day and relaxed scenarios.
   *Done.*
4. **Findings 9 and 10, or go straight to 12.** *Done: finding 12.*
5. **Finding 5**, then **13**: raise the caps.

Every lossless step is verified the way step 1 was: evaluate the same
variants before and after, and require identical schedules.

### Step 1 results

| Scenario (3 variants each) | Before | After | Schedules |
|---|---|---|---|
| Demo roster | 23.7–28.7 s | 9.9–12.8 s | identical |
| Nobody available one Wednesday | 15.2–27.7 s | 6.3–12.1 s | identical |
| One-day-gap relaxation on | 63.5–85.8 s | 32.2–42.6 s | identical |

Default budgets, four variants evaluating at once on a 4-core container.

### Step 2 results

Evaluation with the proven bounds in place of the (1, 1) targets, against
step 1 (the same 3 variants per scenario as above):

| Scenario | Step 1 | Step 2 | Final (backup, main, total) spreads |
|---|---|---|---|
| Demo roster | 9.9–12.8 s | 13.1–19.4 s | 2 of 3 variants improved to total 0; all at their bounds |
| Nobody available one Wednesday | 6.3–12.1 s | 6.5–12.2 s | identical schedules, already at their bounds |
| One-day-gap relaxation on | 32.2–42.6 s | 30.9–41.3 s | identical schedules, already at their bounds |
| One nurse on two weeks' leave (demo budgets) | 54.6–61.1 s | 54.6–61.0 s | identical schedules; bounds out of reach, so no pass stops early, as before |

The extra time on the demo roster is the full-period refill finding the
better schedules. Where a variant already reaches its bounds, nothing
changes. Where it cannot, the passes run to their own limits, as they
always did when (1, 1) was out of reach. One case is new: a variant within
(1, 1) but above its bounds now also runs the full-period refill, which it
used to skip. Under the shipped budgets (800 s per attempt, see the
README's known limitations) that pass can run long when complete refills
are hard to find. Steps 3 and 4 make it far cheaper, or replace it.

### Step 3 results

Each variant evaluated with default budgets on the step-2 code and on the
step-3 code, four at a time. The scenarios add a pinned Main and Backup, a
pinned pair just after the window, and shifts worked just before it
("edges"), and the midweek-pair spacing settings:

| Scenario (variants) | Step 2 | Step 3 | Speed-up | Schedules |
|---|---|---|---|---|
| Demo roster (8) | 8.2–22.1 s | 1.2–2.7 s | 7.8× | identical |
| Nobody available one Wednesday (8) | 8.1–14.1 s | 1.1–1.9 s | 7.7× | identical |
| One-day-gap relaxation on (8) | 28.7–40.1 s | 3.6–5.1 s | 7.9× | identical |
| Midweek-pair relaxations on (8) | 30.3–40.8 s | 4.0–5.4 s | 7.6× | identical |
| Edges (1) | 133 s | 12.9 s | 10.3× | identical |

Against the code before step 1, the demo roster is now about 10× faster.

The other 7 edge variants did not finish on either version within the
run's time limit, which led to the [step 2
follow-up](#step-2-follow-up-bounding-the-extra-refill).

Searches limited by wall time can in principle find more on faster code,
because they get further within the same limit. None did here: the leave
scenario, whose full-period refill attempts stop on their time limit, gave
identical schedules too.

### Step 2 follow-up: bounding the extra refill

Step 2 lets the full-period refill run whenever the spreads are above their
proven bounds, including when both are already within (1, 1), where it
never ran before. On a scenario with pinned cells and shifts just outside
the window, some of its variable orders thrash: chronological backtracking
through all the weekdays never completes a fill. The shipped budgets (800 s
per attempt, up to 54 attempts) then let a single variant run for hours.
The same variant took 2 s under the old (1, 1) rule.

The full-period refill now has two budgets:
- **Above (1, 1)**, where it always ran, it keeps the shipped budgets.
- **Within (1, 1) but above the bounds** (the case step 2 added), each
  attempt gets at most `full_period_extra_attempt_time_ms` (2 s) and the
  whole pass `full_period_extra_time_ms` (20 s).

So nothing searched before step 2 is cut; only the added search is bounded.
Successful attempts on these rosters finish in under a second, and the
attempts that thrash are the ones that hit the limit.

| Scenario (variants) | Before | After |
|---|---|---|
| Edges, default budgets (8) | 1 finished in 133 s; 7 still running after 25 min | all 8 in 1.3–22 s: 4 at total spread 0, 4 at 2 (the old rule's result) |
| Demo sample from finding 7 (8) | all at (1, 1, 0) | all at (1, 1, 0), 1.1–2.5 s each |

Four edge variants stay at total spread 2 after using the whole 20 s. They
are no worse than under the old rule, but it is not known whether 0 is
reachable for them. The exact weekday solve (finding 12) would settle it,
and makes this budget unnecessary. Tests: `tests/test_spread_lower_bounds.py`
checks which budgets the worker passes, and that the pass stops at its total
time.

