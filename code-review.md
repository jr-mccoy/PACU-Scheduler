Fix order

Phase 1 — Stop state corruption and silent wrong behavior

These are the highest-risk bugs because they can produce wrong schedules or corrupt persistent history.

1. Fix tracker loss in iterative_window_refill_rebalance()


2. Fix WeekendHistory derived-state corruption


3. Fix manual violation count being overwritten


4. Make schedule generation respect user rotation-violation choices



Phase 2 — Make optimization behavior internally consistent

These bugs cause the solver to optimize one thing and rank by another.

5. Make profiling use the exact same solver settings as normal mode


6. Fix dead rot_viol metric


7. Fix ScheduleQuality / tracker objective mismatch



Phase 3 — Normalize data handling

These are correctness/consistency fixes that reduce misleading output.

8. Fix partial pre-scheduled weekend inconsistency


9. Unify empty-slot counting


10. Fix PDF rendering of empty assignments


11. Fix debug log fragmentation




---

Phase 0: Put tests in place first

Before changing logic, add characterization tests. This code has a lot of coupled mutable state, so you need a safety net.

Add test groups

A. Weekend history state integrity

Use a temp SQLite DB and assert all three are consistent after every operation:

weekend_assignments

weekend_rotation_history

rotation_violation_stats / rotation_violation_dates


Test cases:

add latest weekend

add older historical weekend

modify latest weekend

modify older weekend

remove weekend

restore backup


B. Tracker correctness

Build a tiny deterministic ScheduleVariant fixture and verify:

begin_iteration() snapshot is restored on revert

global best updates when a better state is found

early success paths do not lose best state


C. Schedule generation mode

Verify:

strict only

strict then relaxed

relaxed allowed immediately for selected nurses


D. Metric behavior

Verify rot_viol, weekend_gap, long_term, and weighted_score actually differ across intentionally different variants.


---

Phase 1: Core correctness fixes

1) Fix tracker loss in iterative_window_refill_rebalance()

Problem

The method can return True inside the inner window loop before calling:

tracker.evaluate_and_commit(...)

tracker.restore_global_best()


That means the improved schedule may never be recorded in the tracker.

Fix

Refactor control flow so the method never returns from inside the inner loop.

How

Inside iterative_window_refill_rebalance():

replace the inner return True with a flag like target_hit = True

break out of the window loop

after the loop, call tracker.evaluate_and_commit(...)

then, if target_hit, call tracker.restore_global_best() and return


Suggested shape

target_hit = False

for pass_idx in ...:
    tracker.begin_iteration(...)
    schedule_changed = False

    for days in windows:
        ...
        if accepted_change:
            schedule_changed = True
            if good_enough():
                target_hit = True
                break

    comparison = tracker.evaluate_and_commit(...)
    if comparison == Comparison.BETTER:
        improved = True

    if target_hit:
        tracker.restore_global_best()
        return True

Validation

Add a test where:

first accepted window reaches target

tracker global best must equal final schedule after return



---

2) Fix WeekendHistory derived-state corruption

This is the biggest structural fix.

Problem

WeekendHistory has canonical data and derived data:

Canonical

weekend_assignments


Derived

_last_patterns

weekend_rotation_history

rotation_violation_stats

rotation_violation_dates


Right now, methods like:

add_assignment()

modify_assignment()

restore()


update canonical and derived state inconsistently.

Fix

Create one authoritative rebuild path for all derived weekend state.

Add a new method

Implement something like:

def _rebuild_derived_weekend_state(self) -> None:
    ...

This method should:

1. reload _assignments


2. rebuild weekend_rotation_history from sorted stored weekends


3. rebuild violation tables from the same stored weekends


4. reload _last_patterns



Important rule

For correctness, historical edit operations should not do incremental pattern updates. They should rebuild from canonical assignments.

That means:

add_assignment()

modify_assignment()

remove_assignment()

restore()


should all end by calling the rebuild routine.

Recommended implementation strategy

Step 1

Keep the DB write simple:

upsert/delete weekend_assignments


Step 2

Run a full rebuild:

clear weekend_rotation_history

clear/rebuild rotation_violation_dates

clear/rebuild rotation_violation_stats

reload in-memory caches


Why this is the right tradeoff

The number of weekend rows is tiny. Full rebuild is cheap and much safer than trying to maintain multiple derived tables incrementally.

Validation

Tests must prove:

editing an old weekend does not overwrite the real latest pattern

removing a weekend restores the correct prior pattern

restore fully restores both assignments and derived state



---

3) Fix WeekendHistory.restore()

Problem

It restores only weekend_assignments and _assignments, but not derived weekend state.

Fix

At the end of restore() call the rebuild method from item 2.

Validation

After restore:

get_assignments()

get_last_pattern()

get_violation_counts()

get_violation_dates()


must all match the backup scenario.


---

4) Fix manual violation count being overwritten

Problem

_handle_set_violation_count() manually sets the count, then immediately calls _recalculate_violation_counts() which overwrites it.

Decide the feature semantics first

Pick one of these and commit to it.

Option A — manual override is temporary but real

remove the recalculation call from _handle_set_violation_count()

document that any later rebuild will recompute from weekend history and overwrite the manual value


Option B — manual override is not supported

remove the menu action entirely

tell the user to edit weekend history and rebuild


Recommendation

Use Option A for now. It is the smallest safe fix.

Exact change

Remove this line from _handle_set_violation_count():

self.weekend_history._recalculate_violation_counts()

Validation

Set a count manually, then read it back. It must persist until an explicit rebuild action.


---

5) Make generation respect user rotation-violation choices

Problem

UI state is collected, but generate_schedule() always does strict first and relaxed only on failure.

Fix

Make generation mode explicit.

Add a generation mode

Use one of these:

"strict_only"

"strict_then_relaxed"

"relaxed_allowed"


Or two booleans if you prefer, but a mode enum/string is clearer.

Suggested API

Add a parameter to generate_schedule():

rotation_mode: str = "strict_then_relaxed"

Then change _generate_weekend_variants()

Behavior should be:

strict_only

only call generate_all_weekend_variants(False)


strict_then_relaxed

current fallback behavior


relaxed_allowed

call generate_all_weekend_variants(True) immediately


UI mapping

In _generate_schedule_with_violations():

if user allowed no violations → strict_only

if user allowed specific nurses or all nurses → relaxed_allowed


Validation

Test that enabling relaxed mode causes the relaxed pass to be used even if strict variants exist.


---

Phase 2: Objective consistency fixes

6) Make profiling mode behavior identical to normal mode

Problem

_evaluate_variant_worker_profiled() uses different solver parameters than _evaluate_variant_worker().

That means profiling changes results.

Fix

Centralize all solver tuning parameters.

Create one shared config object

Add module-level constants or a dataclass, for example:

@dataclass(frozen=True)
class VariantEvalParams:
    gap_fill_iterations: int = 300
    rebalance_iterations: int = 1500
    window_weeks: int = 3
    window_passes: int = 650
    window_time_limit_ms: int = 800000
    window_node_limit: int = 750000
    full_max_orders: int = 1000
    full_time_ms: int = 800000
    full_nodes: int = 1500000

Then both worker functions use the same values.

Validation

Run one variant through both workers with profiling off/on and compare:

final stats

final schedule

weighted score


They should match.


---

7) Fix dead rot_viol metric

Problem

_rotation_violation_score() sums historic violations over nurse_counts, but nurse_counts includes all nurses, so the value is effectively constant.

Fix

Make the metric depend on the actual candidate.

Decide intended meaning

Pick one:

Option A — score only nurses assigned weekends in the candidate

This is probably the cleanest.

Implementation:

extract weekend-assigned nurses from sched_df

sum historic_viol[n] only for those nurses


Option B — weight by number of weekend assignments in candidate

This gives more signal:

if a nurse appears on two weekends, they count more


Recommendation

Use Option B.

Example implementation

Build from weekend rows in sched_df:

detect Friday/Saturday/Sunday assignments

collect unique weekend appearances by nurse

compute weighted historic penalty


Validation

Construct two candidates using different weekend nurses and verify rot_viol differs.


---

8) Fix ScheduleQuality.from_variant() so it computes what it claims

Problem

history_penalty is hardcoded to zero.

Fix

Either compute it or remove it.

Recommendation

Compute it properly if you want it in tracker decisions. Otherwise remove both history_penalty and weekend_penalty from ScheduleQuality.

Because you already use weekend and long-term metrics in final ranking, the cleanest design is:

Option A — full alignment

pass scheduler into all trackers

compute weekend and long-term penalties in ScheduleQuality

compare schedules using the same quality tuple during search and final ranking


Option B — split responsibilities

search only optimizes gaps/spreads/rotation

final ranking handles weekend/long-term

then remove weekend/history fields from ScheduleQuality to avoid lying about the search objective


Recommendation

Use Option A if you want coherent optimization.

How

In worker functions:

tracker = BestStateTracker(var, scheduler=<scheduler reference>)

Then implement real history_penalty calculation inside ScheduleQuality.from_variant().

Because workers currently only receive variant, you will need either:

to pass enough scheduler context into the variant, or

to pass a lightweight scoring helper object to the worker


Validation

Create two equal-gap/equal-spread variants with different weekend-gap penalty and ensure tracker comparison changes.


---

Phase 3: Data consistency fixes

9) Fix partial pre-scheduled weekend inconsistency

Problem

Weekend tracking can say one thing while actual Fri/Sat/Sun cells say another if the weekend is only partially prefilled.

Fix

Before accepting an (fsf_nurse, sfs_nurse) pair, validate it against the full weekend block.

Add a validator

Create a helper like:

def _weekend_pair_matches_existing_cells(self, weekend_start, fsf_nurse, sfs_nurse, schedule) -> bool:
    ...

It should check all 6 cells implied by FSF/SFS:

Fri main = fsf

Fri backup = sfs

Sat main = sfs

Sat backup = fsf

Sun main = fsf

Sun backup = sfs


For each prefilled cell:

if empty: ignore

if non-empty and mismatched: reject pair


Apply it in

_get_valid_nurse_pairs()

or immediately before clone.assign_weekend(...)


Validation

Build a weekend with only Saturday main prefilled inconsistently and ensure the pair is rejected.


---

10) Unify empty-slot counting

Problem

Some paths use .isna(), others use is_empty().

Fix

Create one helper and use it everywhere.

Add helpers

def empty_mask(df: pd.DataFrame) -> pd.DataFrame: ...
def count_empty_slots(df: pd.DataFrame, columns=("main", "backup")) -> int: ...

Use these in:

_count_weekday_gaps

ScheduleVariant.count_gaps

both worker fallback paths

weekly gap counts if you want total consistency


Validation

Feed in cells containing:

None

np.nan

""

"   "


All must be counted as empty.


---

11) Fix PDF export of empty cells

Problem

str(None) becomes "None" in the PDF.

Fix

Use the emptiness helper before rendering.

Change

In _draw_week_rows():

raw_main = sched_df.at[dt, "main"] if dt in sched_df.index else None
main = "" if is_empty(raw_main) else str(raw_main)

Do the same for backup.

Validation

Export a schedule with blank cells and visually verify there is no literal "None".


---

12) Fix debug log fragmentation

Problem

log(kind, payload) creates a new filename on every write.

Fix

Create one per-run filename per kind.

Simple implementation

Define a module-level cache:

_LOG_FILES: dict[str, str] = {}

Then:

if kind not in _LOG_FILES:
    _LOG_FILES[kind] = f"{kind}_dump_{_ts()}.log"
fname = _LOG_FILES[kind]

Validation

Multiple log calls in one run should append to one file.


---

Recommended refactors while touching this code

A. Add a derived-state rebuild API to WeekendHistory

This is the most important structural cleanup.

Suggested methods:

_rebuild_derived_weekend_state()

_rebuild_last_patterns()

_rebuild_violation_tables()


B. Centralize solver parameters

Use one immutable parameter object shared by:

_evaluate_variant_worker

_evaluate_variant_worker_profiled


C. Centralize quality scoring

Have one place define:

search quality

ranking quality


Right now it is split and partially duplicated.


---

Concrete patch sequence

Patch 1

Add tests for tracker and weekend-history integrity.

Patch 2

Fix iterative_window_refill_rebalance() early return.

Patch 3

Add WeekendHistory._rebuild_derived_weekend_state().

Patch 4

Change these methods to use rebuild:

add_assignment()

modify_assignment()

remove_assignment()

restore()


Patch 5

Remove the self-overwriting manual violation count recalc.

Patch 6

Make generate_schedule() accept and honor rotation mode.

Patch 7

Centralize worker tuning parameters and unify profiling/normal behavior.

Patch 8

Fix rot_viol to depend on actual weekend usage in sched_df.

Patch 9

Either:

pass scheduler context into tracker and compute real penalties, or

simplify ScheduleQuality to only include what the tracker truly uses.


Patch 10

Add weekend-block consistency validation for pre-scheduled weekends.

Patch 11

Unify empty counting and fix PDF rendering.

Patch 12

Fix debug log file naming.


---

Acceptance criteria

You’re done when these are all true:

Editing an old weekend does not corrupt last_pattern.

Restoring weekend history restores assignments, last patterns, and violation stats together.

Manual violation count edits persist until an explicit rebuild.

User-selected rotation-violation settings actually change generation mode.

Profiling on/off yields the same schedule for the same inputs.

rot_viol varies across meaningfully different candidates.

Tracker never loses a better schedule because of an early return.

Partially pre-scheduled weekends cannot silently create contradictory FSF/SFS tracking.

Empty cells are counted consistently and do not render as "None".



---

Strong recommendation

If you only do three things first, do these:

1. Rebuild all derived weekend state from canonical assignments


2. Fix tracker early return in iterative_window_refill_rebalance()


3. Unify solver parameters between profiling and normal mode



Those three remove the biggest sources of silent wrongness.

---

## Package migration checklist (facade → owned modules)

- [x] `scheduler/domain.py`: owns `WeekendPattern` and `SchedulerConfig`; legacy-backed types remain temporarily imported.
- [x] `scheduler/repositories.py`: owns `DatabaseMixin`; legacy-backed repository classes remain temporarily imported.
- [x] `scheduler/engine.py`: owns worker tuning config (`WorkerTuningConfig`, `WORKER_TUNING`); engine entrypoints still temporarily imported from legacy.
- [x] `ui/screens/*.py` named modules now own screen compatibility subclasses; `*_screen.py` files reduced to wrappers.
- [x] `ui/dialogs/*.py` named modules now own dialog compatibility subclasses; `*_widget.py` files reduced to wrappers.
- [ ] Follow-up: remove temporary legacy imports once direct implementations are extracted from `scheduler/legacy_core.py` and `ui/legacy.py`.
