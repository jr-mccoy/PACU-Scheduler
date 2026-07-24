# System logic audit

Full read-through of `scheduler/`, `cli/`, and the GUI worker/save paths in
`ui/`, looking for logic errors, silent-failure modes, and improvement
opportunities. Findings are ordered by severity. Each entry names the exact
call site and states how it was verified (read, grep, or executed).

Baseline: `pytest -q` passes 85 tests. The only failure in this environment
(`test_legacy_import_contracts`) is `libEGL.so.1` missing for PySide6, not a
code defect.

---

## Critical — silent data loss

### C1. Saving a generated schedule never writes new weekends to history

`cli/nurse_scheduler_ui.py:1640` and `ui/dialogs/variant_review_dialog.py:227`
both persist the chosen schedule's weekends with
`weekend_history.modify_assignment(...)`.

`WeekendHistoryService.modify_assignment` (`scheduler/history_services.py:44`)
issues `UPDATE weekend_assignments SET ... WHERE weekend_start = ?`. For a
weekend that is not already a row in `weekend_assignments` — i.e. every weekend
in a newly generated schedule — the UPDATE matches zero rows and does nothing.
`add_assignment` is the insert path (`INSERT OR REPLACE`).

Verified by execution against a copy of `nurse_schedule.db`:

```
rows before/after modify_assignment on a new weekend: 26 26
rows after add_assignment on the same weekend:        27
```

The CLI still counts and reports the write (`updated_count += 1`, then
"Updated N weekend assignments in history"), so the failure is completely
silent. The GUI path has no counter at all.

Impact is not cosmetic. README states weekend history is canonical: it drives
`get_last_pattern` (rotation alternation), `get_last_weekend_before`
(`_check_weekend_gap_constraints`), `get_weekends` (`_weekend_gap_penalty`,
`nurse_weekend_lists`, `_collect_pre_window_worked_days`), and the violation
tables. Every subsequent schedule run plans as if the saved weekends never
happened.

**Fix:** use `add_assignment` in both save paths. `modify_assignment` should be
reserved for the explicit "edit an existing weekend" screens
(`cli/nurse_scheduler_ui.py:644`), and should arguably log a warning when
`cursor.rowcount == 0`.

### C2. GUI falls back to publishing unevaluated schedules

`ui/worker_threads.py:174-210`. If the executor raises, or if every future
raises (per-future exceptions are swallowed by `except Exception: pass` at
line 171), `candidate_schedules` ends up empty and the code builds "candidates"
directly from the raw variants:

```python
except Exception:
    candidate_schedules.clear()

if not candidate_schedules:
    for i, var in enumerate(variants):
        df = var.state.schedule.copy()   # weekends only — no weekday assignment ran
```

Those variants have only weekend cells filled; `assign_weekdays`, gap-fill,
rebalance and window-refill never ran. They are then scored, ranked, and shown
to the operator as the top 5 candidates, indistinguishable from real results.

Two secondary problems in the same block:
- `stats["gaps"]` uses `df[["main","backup"]].isna().sum().sum()`, which
  disagrees with the `is_empty()` semantics used everywhere else (it misses
  `""` and whitespace cells).
- `except Exception: pass` around `fut.result()` discards the traceback, so the
  operator gets no signal that evaluation failed.

**Fix:** emit `self.error` instead of fabricating candidates, and log the
per-future exception.

---

## High — logic errors

### H1. Final variant ranking is nondeterministic on ties

`scheduler/engine.py:1465`:

```python
candidate_schedules.sort(key=lambda tpl: tpl[1]["weighted_score"])
```

`candidate_schedules` is appended in `as_completed()` order
(`engine.py:1360`, `1391`; same in `ui/worker_threads.py:166`), which varies
run to run. `list.sort` is stable, so any tie in `weighted_score` resolves to
whichever worker happened to finish first.

Ties are not exotic: `weighted_scores_from_rows` maps a metric to `0.0` when
`hi == lo` across the candidate set, so on a small or homogeneous variant pool
several metrics collapse to zero for everyone. If a user zeroes all
`scoring_weights`, *every* variant scores exactly 0 and the entire ranking is
arbitrary.

This undercuts the determinism the rest of the codebase works hard for —
`sorted(nurses, key=str.casefold)` "for deterministic ordering across
processes/runs" (`engine.py:130`), the four-tier deterministic tie-break in
`_select_best_candidate`, fixed RNG seeds in `gen_full_orders`.

**Fix:** `key=lambda tpl: (tpl[1]["weighted_score"], tpl[0])`.

### H2. `WindowRefillOptimizer` mislabels its iteration baseline and can wipe the tracked best

`scheduler/optimization/window_refill.py:161-168`:

```python
if created_tracker:
    tracker.begin_iteration(f"[WindowRefill] Pass {pass_idx}")
else:
    tracker._iteration_snapshot = ctx.StateSnapshot.capture(
        ctx, tracker.get_global_best_quality() or tracker.initialize(),
    )
    tracker._iteration_quality = tracker._iteration_snapshot.quality
```

Two defects:

1. The snapshot captures the **current** state but stamps it with the **global
   best** quality. `evaluate_and_commit` then compares the post-pass quality
   against the global-best number rather than the pass-start number. Today the
   two happen to coincide (every prior phase ends in `restore_global_best()`),
   so this is latent rather than active — but it is an invariant held by
   coincidence across three separate files.
2. `tracker.initialize()` inside the `or` fallback is not a read. It
   recomputes quality from the *current* state and **overwrites
   `_global_best`/`_global_best_quality`** (`domain.py:463`). Reaching it
   discards the best schedule found so far.

There is no reason for the branch: `begin_iteration()` is exactly this
operation done correctly, and it is what `iterative_rebalance_no_revert` and
`iterative_gap_fill_no_revert` call for passed-in trackers.

**Fix:** call `tracker.begin_iteration(...)` unconditionally and delete the
private-attribute poke.

### H3. `import scheduler` crashes on a non-numeric `DEBUG_SCHED`

`scheduler/debug.py:24`:

```python
_DEBUG = bool(int(os.getenv("DEBUG_SCHED", "1")))
```

Verified by execution — `DEBUG_SCHED=true` raises
`ValueError: invalid literal for int() with base 10: 'true'` at import of
`scheduler.debug`, taking the whole package down. `scheduler/runtime.py:18`
already has `_env_flag()` written for precisely this. Same pattern at
`ui/worker_threads.py:19` (`NSCHED_DEBUG_VARIANTS`).

**Fix:** route both through `_env_flag`.

### H4. Heavy debug instrumentation is opt-out, not opt-in, and sits in the hottest loop

- `scheduler/debug.py:24` — `DEBUG_SCHED` defaults to `"1"`.
- `ui/platform.py:96` — `assignment_debug_enabled` defaults to `True`.
- `scheduler/domain.py:694` — `SCHEDULE_VARIANT_DEBUG` defaults to `"1"`.

So importing `scheduler` creates `assignment_debug_<ts>.jsonl` + `.csv` in the
CWD unconditionally, and `_log_assignment_debug` (`domain.py:1087`) runs on
**every slot decision**. Each call serialises all main counts, all backup
counts, all totals, both 30-day history dicts, and per-candidate stats, then
`flush()`es both handles under a global lock (`debug.py:118-125`).

Two consequences beyond throughput:

- The filename is `%Y%m%d-%H%M%S`, generated at module import in each
  `ProcessPoolExecutor` child. Workers forked in the same second get the *same*
  filename, open it `"a"`, and append concurrently guarded only by a
  per-process `threading.Lock` — interleaved and corrupt CSV rows.
- `BestStateTracker` `print()`s on every `initialize` / `begin_iteration` /
  `evaluate_and_commit` (`domain.py:466`, `480`, `511`, `528`, `539`) with no
  flag gating it at all. With `rebalance_iterations=1500` per variant and up to
  500 variants, that is unbounded stdout.

**Fix:** default all three to off; gate the tracker prints behind
`_console_debug`; include the PID in the debug filename.

### H5. `last_assignment` is maintained everywhere and read nowhere

Grep across the repo confirms no decision path reads
`state.last_assignment` — the one consumer,
`_check_weekend_gap_constraints`, takes it as a parameter explicitly marked
`# kept for signature compatibility; not used` (`engine.py:614`). The only
other read is a debug print (`engine.py:1002`).

It is nonetheless maintained at real cost:

- `_update_last_assignment_dates` (`domain.py:1800`) does a full-schedule
  boolean scan **per nurse** and is called from `assign_weekend`,
  `assign_weekdays`, `_initialize_pre_scheduled_slots`, `_assign_slot_sequence`,
  and the tracker's `initialize`.
- `revert_assignment` (`assignment.py:68`) does another full-schedule scan on
  every rollback — and rollbacks are the inner loop of every backtracking
  search.
- It is copied by `ScheduleState.clone`, `StateSnapshot.capture/restore_to`, and
  `WeekBackup`.

Related: `_initialize_nurse_tracking` (`engine.py:154`) seeds
`last_assignment[nurse]` from `weekend_history.get_last_weekend_before(...)`,
but `ScheduleVariant._initialize_pre_scheduled_slots` immediately calls
`_update_last_assignment_dates()`, which recomputes purely from in-window rows
and resets every unassigned nurse to `None`. The historic seed never survives
construction. (Pre-window spacing is correctly handled by the separate
`pre_window_worked` frozensets, so nothing is broken — but the seeding code is
misleading.)

**Fix:** either delete the field and its maintenance, or restore it to a
consumer. As-is it is pure overhead plus a dead seed that reads like a live
constraint.

---

## Medium

### M1. `PreScheduler` writes unnormalised dates

`repositories.py:1015` inserts `date_str` verbatim, while `_load_assignments`
(line 992) normalises on read and `remove_assignment` (line 1026) deletes on the
**raw** string. A caller passing `2026-7-1` stores a row that
`get_assignments_in_range` will happily return (it normalises after load) but
`remove_assignment("2026-07-01")` will never delete. Every other repository
normalises before writing (`AssignmentHistory.update_history`,
`WeekendHistoryService.*`).

**Fix:** `DateUtils.normalize_date(...).strftime("%Y-%m-%d")` in both methods.

### M2. sqlite connections are never closed outside `DatabaseMixin`

`DatabaseMixin.get_db_connection` closes correctly in its `finally`. Everything
else uses `with sqlite3.connect(...) as conn:` — which commits or rolls back but
**does not close**. Affected: `_ensure_violation_table`,
`WeekendHistory._load_assignments`, `_load_last_patterns`,
`get_violation_dates`, `get_violation_summary`, `get_violation_counts`, all of
`history_services.py`, and all four `PreScheduler` methods. Connections are
reclaimed only by GC.

Compounding this: `_run_command` reloads and rebuilds *everything* per call
(`history_services.py:142-157` deletes and repopulates
`weekend_rotation_history` plus both violation tables, then re-reads all
assignments). The save paths call it once per Friday in a loop, so a 3-month
schedule performs ~13 full history rebuilds and ~13 full reloads.

**Fix:** wrap in `contextlib.closing`, and add a batch entry point so a save is
one transaction plus one rebuild.

### M3. `AssignmentHistory.get_counts` reads a cache bounded by *today*

`_load_history` filters `WHERE sh.date >= ?` using a cutoff of
`today - history_duration_months` (6 months). `get_counts` then filters that
in-memory cache by the caller's window. `NurseScheduler._compute_historical_counts`
asks for a window relative to `start_date`, so generating a schedule for a
period more than ~6 months in the past silently yields all-zero history and the
`long_term` scoring dimension quietly becomes a no-op.

Note the inconsistency: `_collect_pre_window_worked_days` (`engine.py:1140`)
queries the DB directly via `get_history()` and is therefore correct, while
`get_counts` reads the truncated cache.

**Fix:** have `get_counts` go to the DB like `get_history` does, or widen the
cache to cover the requested window.

### M4. `generate_schedule` writes PDFs into the CWD as a side effect

`_export_top_variants_as_pdfs` (`engine.py:1467`) unconditionally writes
`schedule_variant_1.pdf` … `_N.pdf` into the process CWD on every call, before
the operator has selected anything, overwriting previous runs. There is no flag
to disable it and no way to choose a directory.

**Fix:** make it opt-in (`export_pdfs: bool = False`) with an `output_dir`
parameter.

### M5. Weekly limits and weekend windows are not configurable

`MAX_MAIN_ASSIGNMENTS_PER_WEEK`, `MAX_TOTAL_ASSIGNMENTS_PER_WEEK`,
`DEFAULT_PRE_WEEKEND_WINDOW` and `DEFAULT_POST_WEEKEND_WINDOW`
(`domain.py:59-62`) are module constants, while every comparable policy knob
(`weekend_gap_days`, `min_days_between_assignments`, the four
post-weekend day/role toggles) lives in `SchedulerConfig` and is surfaced in the
settings dialog. These four are equally policy and equally likely to differ
between units.

**Fix:** move them onto `SchedulerConfig` with the current values as defaults.

### M6. Worker parallelism defaults are inconsistent

`generate_schedule(max_workers=2)` (`engine.py:1256`) versus the GUI's
`min(8, os.cpu_count() or 1, total)` (`ui/worker_threads.py:151`). The CLI is
capped at 2 workers on any machine for no stated reason.

---

## Performance

### P1. Full permutation enumeration over week slots — the dominant cost

Both `_try_week_permutations_no_revert` (`domain.py:2455`) and
`_fill_week_with_permutation` (`domain.py:2709`) iterate
`itertools.permutations` over every modifiable `(day, role)` slot in a week. A
full Mon–Thu week with nothing pre-scheduled is 8 slots → **40,320
permutations**, each running `_assign_slot_sequence` over 8 slots, each of which
rebuilds an eligible domain by looping all nurses through spacing, weekly-limit
and weekend-window checks.

The exit conditions make the *worst* case the *common* case:

- `_try_week_permutations_no_revert` breaks on the first improving permutation.
  When no improvement exists — the steady state once the schedule is good — it
  runs all 40,320.
- `_fill_week_with_permutation` breaks only when `best_remaining == 0`. A week
  with one genuinely unfillable slot runs all 40,320.

Multiply by weeks in the horizon, by gap-fill passes (`gap_fill_iterations=300`),
by rebalance iterations (up to 1500, though it breaks on first no-progress), by
up to 500 surviving variants.

The codebase already contains the right tool: `WindowRefillOptimizer.backtrack_window`
is a proper MRV backtracking search with forward-checking, time and node budgets.
`gen_full_orders` demonstrates the other viable approach — a curated ~30
orderings instead of `n!`.

**Fix:** replace both permutation loops with the existing backtracking search,
or cap enumeration at a sampled subset with an explicit log line when truncated.

### P2. Redundant early-stop, and a search that never explores

`domain.py:2477-2487`:

```python
if improved and best_tuple[0] <= 1 and best_tuple[1] <= 1:
    ...
    break
if improved:
    ...
    break
```

The first branch is fully dominated by the second — any `improved` breaks. The
docstring says "Try all permutations of modifiable `(date, role)` slots for one
week and keep lexicographic improvements", but the implementation is
first-improvement, not best-improvement. Either the docstring or the loop is
wrong; the tracking of `best_tuple` / `best_state` implies best-improvement was
intended.

### P3. `_check_weekend_gap_constraints` rescans the whole index per nurse per weekend

`engine.py:635` builds `prior_fridays` by iterating the entire schedule index,
then walks it backwards checking three cells each — and this runs for every
nurse × every weekend × every variant in the beam. `state.nurse_weekend_lists`
is already a sorted per-nurse Friday list maintained exactly for this
(`_get_neighboring_fridays` uses `bisect` on it).

### P4. Smaller hot-path items

- `_update_last_assignment_dates` and `revert_assignment`: full-schedule scans
  per nurse / per rollback, for a field nothing reads (see H5).
- `get_weeks` (`domain.py:1841`): `(week_start + timedelta(days=i)) in weekdays`
  is a linear scan of a list inside a nested loop. Cached, so bounded — but
  should be a set.
- `OrderGenerator.gen_full_orders` (`ordering.py:111`): `mrvl.sort(key=lambda item: domain_size(...))`
  recomputes `eligible_domain` on every comparison. Precompute into a list of
  `(size, item)` first.

---

## Dead code

Verified unreferenced across `scheduler/`, `cli/`, `ui/` and `tests/`:

| Symbol | Location |
| --- | --- |
| `ScheduleVariant.calculate_imbalance` | `domain.py:1849` |
| `ScheduleVariant._spread_main_backup` | `domain.py:2144` |
| `ScheduleVariant._days_to` | `domain.py:1810` |
| `ScheduleVariant._is_nurse_assigned_to_next_weekend` (+ its sole helper `_get_next_weekend_dates`) | `domain.py:2043`, `2034` |
| `ScheduleVariant.assign_nurses_to_weekdays` | `domain.py:1932` |
| `ScheduleVariant._validate_post_weekend_assignment` | `domain.py:1665` |
| `ScheduleVariant._build_full_varlist` | `domain.py:2282` |
| `NurseScheduler.export_weekend_variants` | `engine.py:1201` |
| `WeekendHistory._calculate_consecutive_violations` | `repositories.py:689` |
| `WeekendHistory._build_nurse_sequences` | `repositories.py:698` |
| `NurseScheduler._rotation_enforced` | assigned `engine.py:905`, `1044`; never read |

`ScheduleVariant._backtrack_window` (`domain.py:2240`) is referenced only by a
`monkeypatch.setattr` in `tests/test_scheduler_characterization.py:352` — the
production call goes straight to `self.window_optimizer.backtrack_window`, so
that test patches a method nothing invokes and asserts nothing about the real
code path.

Also unreachable:

- `_weekend_gap_penalty`, `engine.py:379-383`: `if not nurse_gaps:` can never
  fire. `timeline` is a sorted `set`, and `horizon_friday` is always strictly
  greater than every element (history is filtered `< start_date`; scheduled
  Fridays are `<= end_date < horizon`), so the horizon is always appended and
  always produces a gap with `curr >= start_date`. The `if gap_days <= 0:
  continue` guard on line 374 is dead for the same reason.
- `iterative_gap_fill_no_revert`, `domain.py:2587`: `improved` is computed and
  then discarded — the function returns `bool(final_quality and
  final_quality.total_gaps == 0)`.

---

## Notes on things that look wrong but are correct

Recording these so a future reader does not re-derive them:

- `_weekday_relaxation_applicable` returning `False` inside pre/post weekend
  windows means `allow_one_day_weekday_gap` genuinely cannot relax spacing next
  to a worked weekend. Intentional.
- `validate_weekday_relative_to_weekend` deliberately falls through from the
  pre-weekend Monday allowance into the post-weekend check, so a Monday that is
  both post- and pre-weekend is still blocked. This is the
  "Block post-weekend Monday even when in the pre-weekend window" fix; the
  comment on `constraints.py:91-94` explains it.
- `main_assignment_counts` / `backup_assignment_counts` include weekend rows, so
  weekday balancing sees each weekend as +2 main / +1 backup (FSF) or +1/+2
  (SFS). This makes the spread metrics measure *total* fairness, which appears
  intended.
- `ScheduleQuality.weekend_penalty` and `history_penalty` are always `0.0`
  inside workers, because `BestStateTracker(var)` is constructed without a
  scheduler (`evaluation/worker.py:66`) — the scheduler is not sent to the
  subprocess. Both are recomputed correctly in the parent by
  `_score_and_rank_variants`. Worth a comment at the construction site.
- `weighted_score` on `ScheduleQuality` is never used by `compare_quality`; the
  docstring at `domain.py:246` already says it is reporting-only.

---

## Suggested order

1. **C1** — one-line change per call site, stops ongoing corruption of the
   canonical history.
2. **C2** — stop presenting unevaluated schedules as results.
3. **H1** — one-line change, restores end-to-end determinism.
4. **H3, H4** — cheap, and H4 is likely a large share of current runtime.
5. **P1/P2** — the real algorithmic win; do it after H4 so the improvement is
   measurable rather than masked by logging I/O.
6. **H2, H5, M1–M6** — correctness hardening and cleanup.
7. Dead-code removal last, as a single mechanical commit.
