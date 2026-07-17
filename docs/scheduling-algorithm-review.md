# Scheduling Algorithm Review — Logic Errors

**Date:** 2026-07-17
**Scope:** `scheduler/engine.py`, `scheduler/domain.py`, `scheduler/constraints.py`,
`scheduler/assignment.py`, `scheduler/scoring.py`, `scheduler/generation/`,
`scheduler/optimization/window_refill.py`, `scheduler/evaluation/worker.py`,
`scheduler/repositories.py`.

Test-suite status at time of review: 67 passed, 1 failed
(`test_legacy_import_contracts` — a PySide/UI import-contract test, unrelated to
the scheduling algorithm).

Findings are ordered by severity. Bugs #1 and #2 were confirmed empirically;
the rest were confirmed by code inspection.

---

## Confirmed bugs

### 1. Stale caches corrupt the weekday tie-breakers during initial assignment

**Location:** `scheduler/domain.py:1258-1261` (`_assign_roles_for_date`)

`_assign_roles_for_date` writes the selected nurse directly:

```python
pick = self._select_best_candidate(eligible, role, date)
self.state.schedule.at[date, role] = pick
counts[pick] += 1
self.state.last_assignment[pick] = date
```

…without calling `_invalidate_weekday_cache()`. Every other mutation path
(`_inc_assign`, `_dec_assign`, `_recalculate_assignment_counts`) invalidates
that cache; this one does not.

**Consequence:** inside the `assign_weekdays()` loop,
`_select_best_candidate`'s tier 1.5 (fewest same-weekday assignments, backed by
`_weekday_counts_cache`) and tier 2 (fewest total assignments, backed by
`_total_counts_cache`) evaluate against data frozen at the moment each cache
was first populated. Only tier 1 (role-specific count) stays live, because it
references the mutated `pd.Series` directly.

**Empirical confirmation:** in a 6-nurse, 4-week scenario, the caches disagreed
with ground truth on **41 slot decisions** during a single `assign_weekdays()`
pass. With tied role counts, the algorithm can repeatedly pick a nurse who
already has more total assignments, or more assignments on that same weekday —
exactly what those tiers exist to prevent. The later rebalance phases partially
repair the damage, but they start from a worse initial solution.

**Fix:** call `self._invalidate_weekday_cache()` after the direct mutation, or
route the write through `apply_assignment` as `_inc_assign` does.

Note: `_try_assign_first_eligible` (`scheduler/domain.py:1934`) has the same
flaw, but it is dead code (no callers).

### 2. Serial fallback duplicates results, then crashes ranking

**Location:** `scheduler/engine.py:1274-1278` (`_evaluate_variants`); same
pattern at `scheduler/engine.py:1302-1311`
(`_evaluate_variants_with_profiling`)

```python
except Exception as e:
    logger.warning(f"ProcessPool failed ({e}); evaluating serially.")
    for idx, var in enumerate(variants):
        candidate_schedules.append(_evaluate_variant_worker((idx, var)))
```

If the `ProcessPoolExecutor` fails **mid-iteration** (e.g. `BrokenProcessPool`
after some futures already completed), `candidate_schedules` already holds the
completed results. The serial fallback then re-evaluates *all* variants and
appends them again, producing duplicate `idx` entries.

**Consequence:** `_score_and_rank_variants` (`scheduler/engine.py:1361`) does
`float(metric_df.loc[idx, "weighted_score"])`. With a duplicated index,
`.loc[idx, ...]` returns a `Series`, and `float(Series)` raises
`TypeError: float() argument must be a string or a real number, not 'Series'`
(verified against the installed pandas). A recoverable pool failure becomes a
hard crash of the whole generation run.

**Secondary issue:** the non-profiled serial loop has no per-variant
`try/except`, so a single failing variant aborts the entire fallback. The
profiled version at `engine.py:1304-1311` handles this correctly.

**Fix:** reset `candidate_schedules = []` (and `all_worker_metrics = []`)
before entering the serial fallback, and wrap each serial evaluation in
`try/except` as the profiled path already does.

---

## Latent / conditional issues

### 3. Backward weekend-gap check does not normalize history dates to Fridays

**Location:** `scheduler/engine.py:663` (`_check_weekend_gap_constraints`)

The backward gap check compares raw dates:

```python
if (weekend - prev_wk).days <= gap_min:
    return False
```

whereas `_weekend_gap_penalty` (`scheduler/engine.py:351`) defensively maps
every history date through `_as_friday` before comparing. Weekend history rows
are keyed by `weekend_start`, normalized to midnight but never snapped to
Friday. If a Saturday/Sunday ever lands in that table, the *hard* gap
constraint is off by 1–2 days — e.g. a legal 16-day Friday-to-Friday gap whose
history entry was recorded as the Sunday reads as 14 days and is wrongly
blocked. Fridays-only data hides this today, but the two code paths disagree
about whether to trust the invariant.

**Fix:** apply `self._as_friday(...)` to `prev_wk_hist` before the comparison.

### 4. `_as_friday` is defined twice on `NurseScheduler`

**Location:** `scheduler/engine.py:158` (`@staticmethod`) and
`scheduler/engine.py:267` (instance method)

The instance method silently shadows the staticmethod. Behavior currently
matches (the surviving definition also handles `None`), but this is a trap:
editing the first definition has no effect. Delete the staticmethod.

### 5. Pre-/post-weekend window overlap handled inconsistently

**Location:** `scheduler/constraints.py:87`
(`validate_weekday_relative_to_weekend`) vs `scheduler/constraints.py:105`
(`validate_weekday_relative_to_weekend_gap`)

In the normal weekday check, being in the pre-weekend window returns
`weekday == 0` **without consulting the post-weekend rules**. A Monday that is
simultaneously 3 days after a worked weekend and 4 days before the nurse's next
weekend is therefore allowed — even though post-weekend rules unconditionally
block Mondays. The gap-fill variant falls through to the post-window check and
correctly blocks it.

This overlap requires back-to-back weekends for the same nurse, which the hard
gap constraint normally prevents — but pre-scheduled pairs bypass that
constraint (`_ensure_pre_scheduled_nurses_included`,
`scheduler/engine.py:856`), and a `weekend_gap_days < 7` configuration would
also expose it. In exactly those cases the nurse can be handed the Monday
between two worked weekends.

### 6. `generate_all_weekend_variants` swallows every exception and returns `[]`

**Location:** `scheduler/engine.py:937-941`

A genuine bug (`KeyError`, malformed data) is indistinguishable from "no
feasible schedule": in `STRICT_THEN_RELAXED` mode the user is then asked to
approve rotation violations in response to what is actually a crash. The
traceback goes only to the debug file, which is disabled unless `NSCHED_DEBUG`
is set. At minimum, log the traceback via `logger.error` unconditionally.

### 7. Plateau counter never resets after hitting its limit

**Location:** `scheduler/domain.py:506-520`
(`BestStateTracker.evaluate_and_commit`)

Once `_plateau_depth` reaches `_max_plateau_depth`, every subsequent neutral
move is reverted for the remaining life of the tracker — `reset_plateau()`
exists but is never called. The plateau allowance is effectively a global
budget rather than a per-plateau one. Minor; flag in case per-plateau behavior
was intended.

---

## Robustness observations (not strictly logic errors)

- **Unbounded variant explosion.** `_process_weekend_variants`
  (`scheduler/engine.py:943`) multiplies variants by the number of valid pairs
  each weekend with no cap, deduplication, or dominance pruning. A small
  6-nurse / 4-weekend probe already produced **720 variants**, each of which
  runs the heavy clone → assign → gap-fill → rebalance → window-refill
  pipeline. Growth is roughly `(pairs)^(weekends)`; a realistic roster with a
  loose gap constraint will stall the run.

- **History is invisible to weekday spacing at the window edge.**
  `_update_last_assignment_dates` (`scheduler/domain.py:1786`) recomputes
  `last_assignment` purely from the in-window schedule, so
  `min_days_between_assignments` cannot see a weekday shift worked the day
  before `start_date`. Worked *weekends* before the window are covered (via
  `nurse_weekend_lists` seeded from history); weekdays are not.

- **Dead code drifting from the live paths.** `_try_assign_first_eligible`,
  `_try_week_assignment`, `_restore_week_assignments`, `_is_within_tolerance`,
  and `_modify_schedule` in `scheduler/domain.py` have no callers, and the
  first of these carries the same cache-staleness flaw as bug #1. Consider
  removing them.

---

## Verification notes

- Bug #1 was reproduced by instrumenting `_select_best_candidate` during
  `assign_weekdays()` on a synthetic 6-nurse / 4-week schedule and comparing
  the cached `_get_total_counts()` / `_weekday_counts_for()` values against
  freshly computed ground truth: 41 of the slot decisions saw stale data.
- Bug #2's crash mode was verified directly: `float()` on the `Series` returned
  by `.loc` over a duplicated index raises `TypeError` under the installed
  pandas version.
- Paths checked and found sound: pair generation vs. prefilled weekend cells
  (`_pair_matches_prefilled_weekend_cells` closes the partial-prefill hole),
  same-day main/backup double-booking (excluded by domain construction in all
  live paths), backtracking undo hygiene in `backtrack_window` /
  `_backtrack_full_order` (assignments are reverted on every failure path,
  including timeout), `WeekBackup`/`StateSnapshot` restore consistency, and
  rotation-violation attribution per branch (`assign_weekend` records repeats
  on the owning clone; `_collect_rotation_violations` dedupes across surviving
  branches).
