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
| 3 | Speedup | The hot path reads and writes a pandas DataFrame cell by cell | Measured: `.at` is 45% after 1–2 | Open |
| 4 | Speedup | Facts fixed once the weekends are fixed are recomputed on every check | Code reading | Open |
| 5 | Speedup | Weekend generation repeats branch-independent work and clones every child | Measured: 6.5 s for 160 variants | Open |
| 6 | Speedup | Smaller redundancies: count recalculations, history scans in ranking | Code reading | Open |
| 7 | Search | Hard-coded (1, 1) spread targets stop the search before it is done | Measured | Fixed |
| 8 | Search | The full-period refill discards improvements that miss the target | Code reading, test | Fixed |
| 9 | Search | The rebalance reaches a small, mostly failing slice of each week | Measured | Open |
| 10 | Search | Neutral (plateau) moves never happen in the rebalance | Code reading, measured | Open |
| 11 | Search | The window refill keeps the first completion it finds, not the best | Code reading | Open |
| 12 | Search | Weeks are independent; the weekday problem can be solved exactly | Measured | Open |
| 13 | Search | Spend the time saved on the caps that do cut the search | — | Open |

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

### 11. The window refill keeps the first completion it finds, not the best

**Where.** `scheduler/optimization/window_refill.py:198`, `:211`.

**What happens.** Each window is cleared and refilled by MRV backtracking,
which returns the first complete fill. That fill is kept only if it
improves the spreads. Other completions of the same window, possibly
better, are never looked at.

**Fix.** Keep searching after the first completion, within the node budget,
and keep the best.

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

### 13. Spend the time saved on the caps that do cut the search

The search is cut in exactly three places:
- `max_week_permutations` (200 orderings per week);
- the (1, 1) targets (finding 7);
- `max_weekend_variants` (the weekend beam). On the demo the beam does not
  bind (160 variants against a cap of 1,000), but it does on larger rosters
  and longer horizons.

Once the speedups land, remove the fixed targets, make the week search
exhaustive (finding 9 or 12), and raise or remove the beam cap.

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
4. **Findings 9 and 10, or go straight to 12.**
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
