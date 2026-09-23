# Scheduler audit

An audit of the scheduling engine (`scheduler/`), and of the GUI and CLI code
that runs it and saves its results, at commit `42446f3`. Line references
point at that commit. The goal was to find logic errors and oversights:
places where the scheduler does something other than what its rules, docs,
or settings say, or where its search wastes its budget.

Each finding ends with the fix it needs, and [the plan](#fix-plan) orders
those fixes into phases. Phases 0–3 are done. Findings 1–7, 9, 13–19
and 22 are **Fixed**, and each fixed finding says how. Finding 10 is closed
as **Won't fix**: fairness keeps raw counts by decision. Every other finding
that could be reproduced has a strict-xfail test in
`tests/test_known_issues.py`, which fails loudly (as an unexpected pass)
when its fix lands.

Severity:

- **P1**: produces wrong schedules, loses data, or misreports what happened.
- **P2**: a rule or fairness objective is not applied as documented, or the
  search spends its budget badly.
- **P3**: dead state, misleading diagnostics, or hygiene.

"Reproduced" means the behaviour was confirmed by running the real code
against a throwaway database (the scripts are summarized under each
finding). "Code reading" means it follows directly from the cited lines.

## Method

- Read every module in `scheduler/` and the generation, review, and apply
  paths in `ui/` and `cli/`.
- Checked the engine against `docs/original_monolithic_scheduler_rules.md`,
  the README's stated policies, and the Settings dialog's descriptions.
- Reproduced each suspected P1 and the main P2 issues with scripts that seed a
  temporary SQLite database (8 regular nurses, 2 PRN, 3 late-shift), then
  drive `NurseScheduler`, `ScheduleVariant`, and the repositories directly.

## Summary

| # | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | P1 | Regenerating a period that is already in weekend history reads its own future | Reproduced | Fixed |
| 2 | P1 | The window's end ignores weekends already committed after it | Reproduced | Fixed |
| 3 | P1 | Cancelling, or closing the review dialog, wipes manual rotation overrides | Reproduced | Fixed |
| 4 | P1 | Saving from the CLI never records new weekends | Reproduced | Fixed |
| 5 | P1 | Weekends cut by the window's edges are left blank, silently | Reproduced | Fixed |
| 6 | P1 | Final ranking can put a schedule with unfilled shifts first | Reproduced | Fixed |
| 7 | P1 | Crashes are reported as "no feasible schedule"; unevaluated variants are shown as results | Code reading | Fixed |
| 8 | P2 | PRN nurses are never scheduled at all | Reproduced | Open (xfail test) |
| 9 | P2 | The 30-day history tie-breaker never runs | Code reading | Fixed |
| 10 | P2 | Fairness metrics ignore how available each nurse was | Code reading | Won't fix (decision) |
| 11 | P2 | Pinned weekend cells are force-included inconsistently and never validated | Code reading | Open |
| 12 | P2 | The weekend-gap setting is a hard, exclusive bound labelled "preferred minimum" | Code reading | Open |
| 13 | P2 | Relaxed rotation relaxes every weekend; neither front end uses strict-then-relaxed | Code reading | Fixed |
| 14 | P2 | Beam pruning ranks by lifetime history and branches before it prunes | Code reading | Fixed |
| 15 | P2 | `rot_viol` does not measure new rotation violations | Code reading | Fixed |
| 16 | P2 | Gap-fill tries every ordering of a week's empty slots, uncapped | Reproduced (591 s) | Fixed |
| 17 | P2 | One unfillable slot disables rebalancing for its whole week or window | Code reading | Fixed |
| 18 | P2 | The rebalance permutation cap only varies the end of the week | Reproduced | Fixed |
| 19 | P3 | Local search cannot see long-term fairness | Code reading | Fixed |
| 20 | P3 | `consec_violations` can never exceed 1 | Reproduced | Open (xfail test) |
| 21 | P3 | `last_assignment` is maintained everywhere and read nowhere | Code reading | Open |
| 22 | P3 | Applying a schedule is not atomic | Code reading | Fixed |
| 23 | P3 | Smaller issues: PDF side effect, history cutoff, unreachable budgets, doc errors | Code reading | Open (PDF side effect fixed) |

## P1 — wrong schedules, lost data, or misreporting

### 1. Regenerating a period that is already in weekend history reads its own future

**Where.** `scheduler/repositories.py:939-941` (`get_last_pattern`),
`scheduler/engine.py:186`, `scheduler/engine.py:673`.

**What happens.** Rotation alternation starts from
`weekend_history.get_last_pattern(nurse)`, which is the nurse's most recent
pattern anywhere in history, not the most recent one before `start_date`. The
backward weekend-gap check calls `get_last_weekend_before(nurse, weekend)`,
which includes history weekends inside the window being generated. Other
consumers do filter to before `start_date` (`get_state_snapshot`, the
`last_assignment` seed, `_weekend_gap_penalty`), so the engine is internally
inconsistent.

**Evidence.** For a 2-week window with no prior history, generation produced
1,320 weekend variants. After recording the first of them in weekend history
(the weekend writes the GUI's Apply makes), regenerating the same window produced 286 variants,
and none was the applied schedule. Every nurse now started the window with
the pattern they worked at the window's end.

**Impact.** Two ordinary workflows give wrong results: re-running a month
after a sick call or a time-off change, and generating months out of order.
Rotation alternates against the wrong pattern, and weekends that were valid
become "infeasible".

**Fix.** Make every history read window-relative. Rotation and gap checks
should use only weekends before `start_date`. Weekends recorded inside the
window are the schedule being replaced, so ignore them during generation. On
apply, delete recorded weekends in the window that the new schedule doesn't
keep. Tell the user before generating that the range overlaps applied
history. See [Phase 1](#phase-1--stop-wrong-schedules-and-data-loss) for how
manual pattern overrides fit in.

**Status: Fixed.** `WeekendHistory.get_last_pattern_before()` gives the
pattern of the last weekend recorded before `start_date`. A stored manual
override counts only when the nurse has no recorded weekend on or after
that date. The backward gap check reads history strictly before
`start_date`. Before generating, the generation screen and the CLI say how
many recorded weekends the range will replace
(`recorded_weekends_in_range`). Tests: `tests/test_window_history.py`.

### 2. The window's end ignores weekends already committed after it

**Where.** `scheduler/engine.py:594-630` (`_get_next_weekend_assignment`),
`scheduler/domain.py:1591-1638` (`_has_sufficient_spacing`),
`scheduler/domain.py:1734-1762` (pre/post-weekend windows).

**What happens.** The forward weekend-gap check only looks at the candidate's
own schedule and at *pre-scheduled* weekends past `end_date`. It never looks
at weekends already in weekend history past `end_date`. The weekday rules
(minimum spacing and the pre-weekend window) never look past `end_date` at
all. `pre_window_worked` solved this for the window's start; there is no
equivalent for its end.

**Evidence.** With `weekend_gap_days=14` and a recorded weekend for nurse A
on Dec 4, `_check_weekend_gap_constraints("A", Nov 27)` returns `True`, so A
can work Nov 27 and again one week later.

**Impact.** Generating November after December has been applied can put a
nurse on a weekend too close to their recorded December weekend, or on the
Thursday right before it.

**Fix.** Pass known assignments after `end_date` into the variant, like
`pre_window_worked` does for the start: weekend history plus pre-scheduled
and `schedule_history` rows for `end_date + max(weekend_gap_days,
min_days_between_assignments, 6)`. Include them in the forward gap check, in
`nurse_weekend_lists` (for the pre-weekend window), and in the spacing check.

**Status: Fixed.** Weekends recorded or pre-scheduled after `end_date` feed
the forward gap check and each nurse's weekend list, which bounds the
pre-weekend window. Shifts committed just after the window
(`post_window_worked`) count toward spacing. Pre-scheduled cells now count
on both sides of the window. Tests: `tests/test_window_history.py`.

### 3. Cancelling, or closing the review dialog, wipes manual rotation overrides

**Where.** `ui/screens/schedule_generation.py:159-160` (backup),
`:287-289` (restore), called at `:298`, `:303`, `:330`, and `:378`;
`scheduler/history_services.py` (`restore_assignments` → `_run_command`).

**What happens.** Before every run the screen snapshots weekend assignments.
When the run is cancelled, fails, finds nothing, or the review dialog is
closed without applying, it calls `WeekendHistory.restore(backup)`. That
deletes and re-inserts every weekend assignment, then *rebuilds* rotation
history and violation tables from them. Generation never writes weekend
history, so the restore protects nothing. But the rebuild discards every
`set_last_pattern` and `set_violation_count` override, which the README says
persist "until an explicit rebuild". It also overwrites any weekend edit made
in another window during the run.

**Evidence.** After `set_last_pattern("A", SFS)` over a derived `FSF`, a
`restore(backup())` returns A to `FSF`.

**Fix.** Remove the backup and restore from the generation screen.
Generation is read-only. Protect Apply by making it a single transaction
instead (finding 22).

**Status: Fixed.** The generation screen no longer snapshots or restores
weekend history. Tests: `tests/test_generation_screen.py`.

### 4. Saving from the CLI never records new weekends

**Where.** `cli/nurse_scheduler_ui.py:1755-1782`.

**What happens.** `_update_weekend_history` calls `modify_assignment`, which
is a SQL `UPDATE ... WHERE weekend_start = ?`. A newly generated weekend has
no row yet, so nothing is written. The CLI still prints "Updated N weekend
assignments" and "schedule saved and weekend history updated". The GUI had
the same bug and was fixed (UI audit #6); the CLI copy was missed.

**Evidence.** `modify_assignment(Nov 6, "A", "B")` on an empty history
leaves `get_assignments() == []`.

**Impact.** The next generation starts from stale rotation history, so
alternation and weekend gaps are wrong. Violation stats never see the new
weekends either.

**Fix.** Save through one shared, transactional apply service in `scheduler/`
used by both front ends. It uses insert-or-replace, like the GUI's fixed path.

**Status: Fixed.** The CLI saves through `scheduler.apply_schedule()`, the
same path as the GUI. Tests: `tests/test_apply_schedule.py`.

### 5. Weekends cut by the window's edges are left blank, silently

**Where.** `scheduler/engine.py:570-586` (`_get_weekends`),
`scheduler/domain.py:1187-1197` (`assign_weekdays`),
`ui/screens/schedule_generation.py:36-48` (`describe_range`).

**What happens.** Weekend generation only covers Fridays whose whole
Friday–Sunday block is inside the window. Weekday filling only covers Monday
to Thursday. Nothing fills a Saturday or Sunday at the start of the window,
or a Friday or Saturday at its end. `describe_range` counts Saturdays, so the
screen reports that edge weekend as included.

**Evidence.** For Sat Oct 3 – Fri Oct 30, 2026, Oct 3, Oct 4, and Oct 30 are
empty in every variant, and only the three complete weekends are generated.

**Impact.** A calendar-month range (1st to 30th or 31st) almost always starts
or ends mid-weekend, so the usual way to pick a range leaves on-call days
uncovered. These cells also count as gaps in every variant. The gap metric
never reaches zero, so gap-fill cannot finish early.

**Fix.** Schedule whole weekends. If the window starts on Saturday or Sunday,
or ends on Friday or Saturday, extend the internal horizon to cover the whole
Friday–Sunday block. Treat any cells already recorded for the overhang as
pre-scheduled. Show the adjusted range on the screen, or have the date
pickers snap to it. Make `describe_range` count only complete weekends.

**Status: Fixed**, as recommended (open decision 5).
`whole_weekend_range()` widens the range to whole weekends. The exception
is a cut weekend that is already recorded, usually by the previous month's
run: the range is not widened over it, and its days inside the range are
pinned to their recorded roles, so consecutive months never reshuffle each
other's boundary weekend. The screen and the CLI show the widened range,
and the screen counts weekends over it. Tests:
`tests/test_edge_weekends.py`.

### 6. Final ranking can put a schedule with unfilled shifts first

**Where.** `scheduler/scoring.py:92-107` (`weighted_scores_from_rows`),
`scheduler/engine.py:1567-1602`.

**What happens.** Each metric is min-max normalized across the candidates,
then weighted, and `gaps` carries only 20% of the weight. Min-max gives a
1-point difference in any metric its full weight, however small the
difference is. The local search optimizes lexicographically with gaps first,
but the final ranking then trades gaps away.

**Evidence.** Candidate 0 has 1 unfilled shift, a weekend-gap penalty of 40,
and a balance of 2. Candidate 1 is fully covered, with a penalty of 41 and a
balance of 3. They score 0.20 and 0.25, so the candidate with the unfilled
shift is ranked first.

**Fix.** Rank by coverage first: sort by `(gaps, weighted_score)`, and
consider `rotation_rep` as a second hard key (see open decisions).
Normalize the remaining metrics against fixed reference scales, not the
candidate pool's min-max, so a trivial difference stays trivial. Show the
gap count prominently in the review dialog.

**Status: Fixed**, in the order set by open decision 2:
1. fewest pattern repeats;
2. then fewest unfilled slots;
3. then the weighted score over the remaining metrics;
4. then candidate index.

`scheduler.scoring.rank_rows()` implements this, and each candidate's
stats carry a `rank`. The weighted metrics are normalized against fixed
minimum scales (`METRIC_SCALES`), so a one-day or one-shift difference no
longer earns a metric its full weight. Repeats and unfilled slots have
no weights any more. The Settings dialogs explain the order, and the
review dialog lists those two metrics first. Tests: `tests/test_ranking.py`.

### 7. Crashes are reported as "no feasible schedule"; unevaluated variants are shown as results

**Where.** `scheduler/engine.py:1025-1031`, `ui/worker_threads.py:255-275`,
`ui/screens/schedule_generation.py:327-341`.

**What happens.**

- `generate_all_weekend_variants` catches every exception, logs it, and
  returns `[]`. That looks exactly like infeasibility. The GUI then shows "No
  schedule satisfies every rule… Things to try: …", which sends the manager
  off relaxing constraints because of a bug. In `STRICT_THEN_RELAXED`, a crash
  even triggers the prompt to allow rotation violations.
- If every variant's evaluation raises, the GUI worker "ranks unevaluated
  variants" and offers them for review. These are weekend-only schedules with
  every weekday empty, presented as options that can be applied.

**Fix.** Let generation errors propagate, or return a structured result that
distinguishes *infeasible* from *error*. This is the first step of the
`WeekendGenerationResult` in `docs/weekend-candidate-generation.md`. Remove
the unevaluated-variant fallback. If no variant evaluates, that is an error
with a traceback.

**Status: Fixed.** `generate_weekend_candidates()` returns a
`WeekendGenerationResult` whose status is `ok`, `infeasible` or `error`.
`generate_all_weekend_variants()` keeps its list return. `generate_schedule()`
raises `GenerationError` on a crash, before any relaxation prompt, and
when no variant could be evaluated. The GUI worker reports both cases
through its error signal, the unevaluated-variant fallback is gone, and
a ranking failure is an error too. Tests:
`tests/test_generation_outcomes.py` and `tests/test_generation_screen.py`.

## P2 — rules not applied as documented

### 8. PRN nurses are never scheduled at all

**Where.** `scheduler/factory.py:195-199`, `scheduler/engine.py:160-161`.

**What happens.** Only non-PRN nurses become `NurseScheduler.nurses`, and
every variant, domain, and metric is built from that list. `prn_nurses` is
stored and never used again. The rules document says PRN nurses are blocked
from *weekend* logic, and the README describes "PRN (as-needed)
eligibility". In practice a PRN nurse can only appear through a manual
pre-scheduled cell.

**Fix.** This needs a product decision (see
[open decisions](#open-decisions)). The recommended behaviour: PRN nurses
join the weekday domain only as a last resort, after every regular nurse,
when a slot would otherwise stay empty. They stay out of the spread and
long-term fairness metrics.

### 9. The 30-day history tie-breaker never runs

**Where.** `scheduler/engine.py:990-997`, `scheduler/domain.py:711-717`,
`scheduler/generation/domain.py:121-122`.

**What happens.** The root `ScheduleVariant` is constructed without
`historical_main`/`historical_backup`, so `hist_main`/`hist_backup` are
empty for every variant. Tie-break step 4 in rules §4.3 ("fewest recent
historical assignments") compares zeros, and so does the matching key in
candidate-domain ordering.

**Fix.** Pass `self._historical_main` and `self._historical_backup` into the
root variant. Add a test that a tie is broken by history.

**Status: Fixed.** The root variant gets the historical counts, and
clones share them. Tests: `tests/test_recent_history.py`.

### 10. Fairness metrics ignore how available each nurse was

**Where.** `scheduler/domain.py:1033-1044`, `scheduler/scoring.py:28-54`,
`scheduler/engine.py:259-275`.

**What happens.** Main, backup, and total spread are raw `max - min` counts
over every non-PRN nurse. A nurse on two weeks of leave holds the minimum
down, so:

- The worker's `(1, 1)` spread targets become unreachable. The expensive
  full-period refill always runs, and the rebalance can only chase the
  maximum.
- `_long_term_score` subtracts that same minimum from everyone, which
  flattens its signal.

The spread fairness ends up measuring who took time off, not whether the
schedule was fair.

**Fix.** Measure fairness against an expected share. For each nurse, compute
the slots they could legally take given their availability. Their expected
count is that share of the slots to fill. Replace the raw spreads with
max/min deviation from expected, in both the local search and the final
ranking. Exclude nurses with near-zero availability from the minimum.

**Status: Won't fix, by decision.** Fairness keeps raw max-minus-min
counts. The consequences above stand, including spread targets that a
nurse on leave can make unreachable. Revisit if those cost too much
search time or rank badly in practice.

### 11. Pinned weekend cells are force-included inconsistently and never validated

**Where.** `scheduler/engine.py:473-554`, `:798-812`, `:928-945`, and
`scheduler/repositories.py:1007-1020`.

**What happens.**

- A pinned Friday cell becomes a fixed FSF/SFS nurse, which bypasses the
  availability, gap, and rotation checks. A pinned Saturday or Sunday cell
  (without an inferable whole weekend) is only enforced by rejecting pairs
  that disagree with it. That pinned nurse must still pass every check to
  appear in a pair at all, so a Saturday pin can make the whole weekend
  infeasible while an equivalent Friday pin always works.
- Pre-scheduled rows are never validated. The same nurse can be in both roles
  on one day, a nurse can be pinned on a day they are unavailable, and
  pinned rows of deactivated nurses still load (`PreScheduler` joins without
  an `is_active` filter).

**Fix.** Derive a weekend's pinned FSF/SFS nurse from any pinned cell (Fri
main, Sat backup, or Sun main mean FSF; the mirror cells mean SFS). Treat
contradictory pins as a locked-data conflict. Run a pre-flight validation of
the pre-schedule, and show its findings before generation starts.

### 12. The weekend-gap setting is a hard, exclusive bound labelled "preferred minimum"

**Where.** `scheduler/engine.py:700` and `:710` (`<= gap_min` rejects),
`ui/dialogs/settings_support.py:106`, `scheduler/domain.py:136` vs
`scheduler/settings.py:22`.

**What happens.** Settings describes `weekend_gap_days` as the "Preferred
minimum days between two weekends". It is actually a hard rule, and an
exclusive one. With the default of 28, a nurse cannot work every fourth
weekend (exactly 28 days apart); the effective minimum is 35 days.
Separately, `SchedulerConfig` defaults the gap to 14 while `SharedSettings`
defaults it to 28, so library callers and the apps get different policies
(the rules doc already flags this).

**Fix.** Decide whether the bound is inclusive. Either way, make the label
state the enforced rule exactly ("Weekends must be more than N days apart"),
and give `SchedulerConfig` its defaults from `SharedSettings.DEFAULTS`.

### 13. Relaxed rotation relaxes every weekend; neither front end uses strict-then-relaxed

**Where.** `scheduler/engine.py:1087-1100`, `ui/worker_threads.py:173-176`,
`cli/nurse_scheduler_ui.py:1699-1716`.

**What happens.**

- In relaxed mode every weekend admits repeat patterns, not just the weekend
  that strict mode could not staff. Only the beam's sort key and the 30%
  `rotation_rep` weight push against extra repeats. With the cap at 0
  (unlimited) the branching grows sharply.
- The GUI calls `generate_all_weekend_variants` directly and reimplements
  evaluation and ranking. The CLI maps to `STRICT_ONLY` or `RELAXED_ALLOWED`.
  The engine's documented default, `STRICT_THEN_RELAXED`, is used by neither,
  and the GUI's copy of the pipeline has already drifted from
  `generate_schedule` (finding 7 is one symptom).

**Fix.** Put generation, evaluation, ranking, progress, and cancellation
behind one engine entry point, and make both front ends call it. Make the
relaxed search minimal: per branch, use strict pairs when they exist and
admit repeats only on the weekends where that branch has none. Keep the
user's confirmation before relaxing.

**Status: Fixed.**
- `NurseScheduler.run_generation()` is the single pipeline. It takes
  stage, progress and cancellation callbacks, can evaluate in threads, and
  returns a `GenerationRun`.
- The GUI worker only maps its callbacks to Qt signals, and
  `generate_schedule()` wraps it for the CLI and scripts. A test checks
  that both rank the same inputs identically.
- As decided (open decision 7), relaxed mode tries each branch's strict
  pairs first and admits a repeat only on a weekend that branch cannot
  staff otherwise. When strict rotation is feasible, relaxed mode yields
  exactly the strict variants.
- The GUI still asks about rotation violations up front and does not
  prompt midway. With repeats admitted only where needed, allowing them
  up front gives the same schedules as strict-then-relaxed would.

Tests: `tests/test_generation_pipeline.py` and
`tests/test_relaxed_rotation.py`.

### 14. Beam pruning ranks by lifetime history and branches before it prunes

**Where.** `scheduler/engine.py:1033-1069`, `:1002-1019`.

**What happens.** Three problems:

- `prune_key` computes weekend imbalance over `nurse_weekend_lists`, which
  holds *every* historical weekend. Imbalance is therefore dominated by
  tenure: a new hire is always the minimum, so the beam steers weekends to
  them whenever the gap rule allows.
- The "largest minimum gap" term also spans all of history, so one short gap
  from years ago fixes its value and it stops distinguishing variants.
- Every surviving variant is cloned once per valid pair *before* pruning, so
  memory peaks at `max_weekend_variants × pairs` full DataFrame copies. With
  the default cap of 1,000 and dozens of valid pairs per weekend, that is
  tens of thousands of copies.

**Fix.** Compute the prune key over a bounded recent window (for example,
`weekend_gap_days × 4` before the start, plus the window itself), and count
gaps only where the later weekend is in the window. Score each (parent,
pair) child before cloning it, keep the best `max_weekend_variants` in a
bounded heap, and clone only the survivors. Record in the result that the
search was capped, as the design doc requires.

**Status: Fixed.**
- Balance counts weekends from four weekend gaps before the start onward.
- The minimum-gap term considers only gaps ending inside or after the
  window.
- Children are scored from parent and pair before cloning
  (`_child_beam_key`, checked against real clones), and only the best
  `max_weekend_variants` are cloned.
- `GenerationRun.search_capped` carries the cap through, and the review
  dialog, the no-results dialog and the CLI tell the user when the cap
  discarded branches.

Tests: `tests/test_weekend_beam.py`.

### 15. `rot_viol` does not measure new rotation violations

**Where.** `scheduler/engine.py:298-329`.

**What happens.** The metric multiplies each nurse's *historic* violation
count by the number of weekend days they work in the candidate. It steers
weekends away from nurses who had repeats in the past, whether or not this
schedule gives them another repeat. With strict rotation, no candidate adds
any repeats, yet this metric still carries 15% of the ranking.

**Fix.** Redefine it as a weighted count of the *new* repeats in this
candidate: each repeat weighted by `1 + prior violations` of the nurse it
lands on. Repeats then go to the nurses who have had the fewest. This
changes what gets optimized, so it needs a product decision.

**Status: Fixed**, as recommended (open decision 3).
- Each new repeat in a candidate counts `1 + that nurse's earlier
  violations`.
- Patterns start from each nurse's last pattern before the window, and
  earlier violations are counted before it too
  (`WeekendHistory.get_violation_counts_before`).
- A manual violation-count override applies unless violations are
  recorded inside the window.

Tests: `tests/test_ranking.py`.

### 16. Gap-fill tries every ordering of a week's empty slots, uncapped

**Where.** `scheduler/domain.py:2696-2765` (`_fill_week_with_permutation`,
the `permutations(...)` call at `:2723`).

**What happens.** Gap-fill clears a week and tries its empty slots in every
order until one ordering fills them all. `max_week_permutations` caps the
rebalance pass but not this one. If any slot in the week cannot be filled
(nobody is available, or every candidate breaks a rule), no ordering
succeeds, so all 8! = 40,320 orderings run. With a one-day-gap relaxation
enabled, they run twice.

**Evidence.** In a week where no nurse is available on Wednesday, one
gap-fill attempt ran 40,320 orderings and took 591 s in this environment.

**Impact.** This is very likely the main cause of the README's "evaluation
is slow" limitation. The per-cell pandas lookups it blames are multiplied by
this loop.

**Fix.** Before searching, find the week's structurally unfillable slots:
those with an empty domain even in an otherwise cleared week. Exclude them
from the search, and report them as unfillable. Then replace the exhaustive
permutation loop with the MRV backtracking the window optimizer already has,
under a node budget.

**Status: Fixed.** A week is first filled by MRV backtracking under
`gap_fill_node_limit` and `gap_fill_time_limit_ms`. Failing that, the best
partial fill over at most `max_week_permutations` orderings is kept.
Unfillable slots (finding 17) are skipped entirely. Tests:
`tests/test_search_budgets.py`.

### 17. One unfillable slot disables rebalancing for its whole week or window

**Where.** `scheduler/domain.py:2484` (rebalance uses a non-partial
`_assign_slot_sequence`), `scheduler/optimization/window_refill.py:86` and
`:198`, `scheduler/domain.py:2340-2341` (full-period refill).

**What happens.** The rebalance, window-refill, and full-period-refill passes
all require every slot in their scope to be filled. One impossible slot makes
every attempt in its week fail, and every window that contains it (windows
are 3 weeks wide). The full-period refill can then never succeed anywhere.
Each attempt still spends its node budget re-proving the same impossibility.

**Fix.** Use the same unfillable-slot precomputation as finding 16. Leave
those slots out of every "must fill" variable list, so the optimizers
rebalance everything around them.

**Status: Fixed.** `ScheduleVariant.compute_unfillable_slots()` runs
once the weekends are fixed. It records the slots with an empty domain
even when every other weekday is cleared; eligibility only shrinks as
cells fill, so these slots can never be filled. Every search skips them.
Each variant reports them as `unfillable`, and they appear in the gap
report, the review dialog ("Impossible slots") and the CLI. Tests:
`tests/test_search_budgets.py`.

### 18. The rebalance permutation cap only varies the end of the week

**Where.** `scheduler/domain.py:2468-2471`.

**What happens.** `islice(permutations(slots), max_week_permutations)` takes
the *first* 200 orderings, and `itertools.permutations` is lexicographic.
Among the first 200 orderings of a week's 8 slots, the first two positions
(Monday main and Monday backup) never change. The capped search only
reshuffles the end of the week.

**Fix.** Sample the capped orderings with a seeded RNG so every position
varies, and keep the result deterministic. Better still, replace the
permutation search with swap/move local search. That searches the same space
at a fraction of the cost.

**Status: Fixed.** `slot_orderings()` yields every ordering when there
are no more than the cap. Otherwise it yields the given order, then
distinct shuffles, seeded per week so runs are deterministic. Rebalance
and gap-fill both use it. Tests: `tests/test_search_budgets.py`.

## P3 — dead state, diagnostics, and hygiene

### 19. Local search cannot see long-term fairness

The worker creates `BestStateTracker(var)` without a scheduler
(`scheduler/evaluation/worker.py:73`), so `weekend_penalty` and
`history_penalty` are always 0 during search. The weekend penalty can't
change during weekday search, so that half is harmless. But the long-term
penalty depends on weekday counts, so only the final ranking ever sees it.
**Fix:** compute the historic overage once in the parent process and pass it
in the work item. Then the tracker can use `history_penalty` as its last
tiebreak.

**Status: Fixed.** The variant carries the historic overage and ships it
to workers; `ScheduleQuality.from_variant` uses it when no scheduler is
passed. Tests: `tests/test_recent_history.py`.

### 20. `consec_violations` can never exceed 1

`_process_nurse_violations` (`scheduler/repositories.py:741`) counts two
violations as consecutive only when they are 7 days apart. A nurse's worked
weekends are always at least `weekend_gap_days` apart, so this never
happens. Three successive repeat violations still report `consec_viol = 1`
on the rotation stats screen. **Fix:** count violations on successive
*worked* weekends of that nurse, whatever the gap between them.

### 21. `last_assignment` is maintained everywhere and read nowhere

`ScheduleState.last_assignment` is copied on every clone, snapshotted,
restored, and recomputed by a full-schedule scan after each weekend
assignment. No decision reads it: `_check_weekend_gap_constraints` documents
its parameter as "not used". `_update_last_assignment_dates` also replaces
the historical seed value with `None`. **Fix:** remove the field and the
parameters that carry it.

### 22. Applying a schedule is not atomic

`VariantReviewDialog._apply` (`ui/dialogs/variant_review_dialog.py:343-349`)
writes one transaction per day and triggers a full rotation rebuild for each
weekend. A failure halfway leaves a half-applied month. It also writes
`NULL` rows for unfilled days, replacing whatever history was recorded for
them. **Fix:** a shared `apply_schedule()` service that writes the whole
window in one transaction, rebuilds rotation history once, and removes
weekends in the window that the new schedule doesn't keep (finding 1).

**Status: Fixed.** `scheduler.apply_schedule()` does all of this in one
transaction with a single rotation rebuild, after checking every name:
- replaces the per-day history for the schedule's dates, removing the
  record for a day nobody works instead of writing an empty row;
- records each Friday's rotation;
- removes any recorded weekend the schedule leaves unpaired.

Tests: `tests/test_apply_schedule.py`, including rollback after a failed
rebuild.

### 23. Smaller issues

- `generate_schedule` always writes `schedule_variant_N.pdf` into the
  current directory (`scheduler/engine.py:1413`). That is a side effect of a
  library call, and the file names collide between runs. Move PDF export to
  the callers. *Fixed in Phase 2: the CLI calls
  `export_top_variants_as_pdfs()` explicitly.*
- `AssignmentHistory` caches history from a cutoff relative to *today*
  (`scheduler/repositories.py:155-159`), not to the scheduling window. A
  window more than `history_duration_months` from today silently gets no
  30-day history for the overage metric. Anchor the cutoff to `start_date`.
- `full_period_max_orders` defaults to 1,000, but `gen_full_orders` can
  produce at most 54 distinct orders, so the budget overstates the search.
- The README says a week has "ten modifiable slots" (10! orderings). The
  rebalance works on Monday–Thursday, which is 8 slots and 8! = 40,320.
- `_is_nurse_available_for_weekend` (`scheduler/engine.py:636`) duplicates
  `_is_nurse_eligible_for_weekend` and has no callers.

## Fix plan

The phases are ordered by risk to real schedules. Each phase is sized to be
one or two reviewable PRs. Every fix lands with a regression test that
reproduces the finding first; the reproductions above map directly onto
tests.

### Phase 0 — safety net

1. Add a `tests/fixtures` helper that seeds a temporary database (roster,
   weekend history, time off, pre-schedule). The reproduction scripts for
   this audit already do this.
2. Add a failing test per P1 finding (1–7) and for findings 8, 9, 16, 18,
   and 20, marked `xfail(strict=True)` so each one flips when its fix lands.
3. Add a fast performance guard: evaluating one variant on a 4-week window
   with an unfillable day must finish within a fixed budget. This test fails
   today because of finding 16.

**Done when** the test suite encodes every finding above that is marked
Reproduced.

**Status: Done.**
- The helper is `tests/scheduling_fixtures.py`: eight regular nurses and
  one PRN, a 14-day weekend gap, and a 25-variant beam, so each test runs
  in seconds.
- The finding-16 guard counts slot orderings instead of timing a run. It
  fails as soon as gap-fill passes `max_week_permutations`, which is fast
  and not flaky on a busy CI runner.
- Findings still open are strict-xfail tests in
  `tests/test_known_issues.py`. Finding 8's test encodes the recommended
  policy (open decision 1) and should be revisited if that decision goes
  another way.

### Phase 1 — stop wrong schedules and data loss

Findings 1, 2, 3, 4, 5, 7, and 22.

1. **Window-relative history** (1).
   - Add `WeekendHistory.pattern_before(nurse, date)`, derived from canonical
     assignments before the date.
   - Honour a manual `set_last_pattern` override only when the nurse has no
     recorded weekend on or after `start_date`. Otherwise the override is
     stale.
   - Change the backward gap check to use history strictly before
     `min(weekend, start_date)`, together with the branch's own weekends.
   - Warn in the GUI and CLI when the range overlaps applied weekends.
2. **Forward horizon** (2). Add `post_window_worked` and future weekend
   Fridays to `ScheduleState`, built from weekend history, the pre-schedule,
   and `schedule_history` after `end_date`. Consult them in
   `_get_next_weekend_assignment`, `_has_sufficient_spacing`, and
   `_get_neighboring_fridays`.
3. **One apply path** (3, 4, 22). Add `scheduler/apply.py` with
   `apply_schedule(db, schedule_df, start, end)`. It writes one transaction,
   uses insert-or-replace, removes weekends in the window that the schedule
   doesn't keep, and rebuilds once. Route the GUI's Apply and the CLI's save
   through it. Delete the backup/restore in `schedule_generation.py`.
4. **Whole weekends** (5). Normalize the horizon to whole weekends inside
   `NurseScheduler`: extend the start back to Friday if it falls on Saturday
   or Sunday, and the end forward to Sunday if it falls on Friday or
   Saturday. Seed overhang cells from recorded history as pre-scheduled.
   Show the adjusted range in `describe_range` and the CLI prompt.
5. **Honest failures** (7). `generate_all_weekend_variants` returns a result
   with `status ∈ {ok, infeasible, error}` (the first slice of the
   design-doc `WeekendGenerationResult`). The GUI shows the error dialog for
   `error` and the infeasibility advice only for `infeasible`. Remove the
   unevaluated-variant fallback in `worker_threads.py`.

**Done when**:

- regenerating an applied window reproduces the applied schedule among its
  candidates;
- cancel and reject leave overrides untouched;
- a CLI save records new weekends;
- a Saturday-to-Friday range has no blank weekend cells;
- a forced exception surfaces as an error, not as infeasibility.

**Status: Done.** Every item above holds and is tested. Differences from
the plan as written:
- The query is `get_last_pattern_before()`, alongside the existing
  `get_last_pattern()`.
- `apply_schedule(db, schedule)` takes its range from the schedule's own
  index.
- For recorded edge weekends, the fix pins their days instead of seeding
  the overhang (see finding 5).
- A range that overlaps recorded weekends is flagged in the generation
  screen's summary and in the CLI.
- The GUI worker also covers the success path with a real
  generate-evaluate-rank run under tight budgets.

### Phase 2 — rank and optimize what the rules say

Findings 6, 9, 10, 13, 15, and 19.

1. **Coverage first** (6). Rank by `(gaps, weighted_score)`. Normalize the
   remaining metrics by fixed reference scales, not the candidate pool's
   min-max. Keep the weights configurable. Show gaps first in the review
   dialog and the CLI listing.
2. **History tie-break** (9). Pass the historical counts into the root
   variant.
3. **Availability-aware fairness** (10). Precompute each nurse's legal slot
   capacity per role. Replace the raw spreads with deviation from expected
   share in `spread_components`, `compute_quality_metrics`,
   `_rebalance_score`, and `_long_term_score`. Base the worker's early-stop
   targets on deviation.
4. **One orchestration path** (13). Add progress and cancellation callbacks
   to `generate_schedule`, and make the GUI worker a thin wrapper around it.
   Implement minimal-repeat relaxed search: per branch, relaxed pairs only
   where strict pairs are empty.
5. **Meaningful `rot_viol`** (15). Implement the weighted-new-repeats
   definition once the product decision is made.
6. **Long-term in local search** (19). Ship the precomputed overage with the
   work item.

**Done when** ranking tests show that a fully covered schedule always beats
one with gaps, that a nurse on leave does not change the spread target, and
that the GUI and CLI produce identical rankings for the same inputs.

**Status: Done**, with the decisions made before starting:
- **Item 1.** Ranking puts fewest pattern repeats ahead of fewest
  unfilled slots (open decision 2), so "a fully covered schedule always
  beats one with gaps" holds among candidates with equally many repeats.
- **Item 3** (availability-aware fairness) was dropped: fairness keeps
  raw counts by decision, and finding 10 is closed as Won't fix.
- **Item 5.** `rot_viol` uses the recommended definition.
- **Item 4.** Relaxed search admits repeats only where needed.

Beyond the plan:
- `generate_schedule()` no longer writes PDFs (part of finding 23).
- The generation run keeps the machine awake in the GUI too, not only
  in the CLI.

### Phase 3 — make the search spend its budget well

Findings 14, 16, 17, and 18.

1. **Unfillable-slot precomputation** (16, 17). Per variant, after weekends
   are fixed, compute the slots with an empty domain in a cleared
   neighbourhood. Exclude them from every "must fill" list, and report them
   in the gap report and the review dialog.
2. **Gap-fill search** (16). Replace `_fill_week_with_permutation`'s
   exhaustive loop with `WindowRefillOptimizer.backtrack_window` in gap
   mode, with a node budget.
3. **Rebalance orderings** (18). Use seeded random sampling of orderings,
   then evaluate swap/move local search as the replacement.
4. **Beam pruning** (14). Use a recent-window prune key, bounded-heap
   selection before cloning, and an "incomplete search" flag in the result.
5. **Budgets.** Re-derive the `WorkerTuningConfig` defaults from measured
   runs, and correct the README's performance section and slot count.

**Done when** the Phase 0 performance guard passes and the demo's run time
drops. Record before and after timings in the README.

### Phase 4 — rules hygiene and cleanup

Findings 8, 11, 12, 20, 21, and 23.

1. **PRN policy** (8), once decided: add a last-resort weekday pool, with a
   Settings toggle.
2. **Pinned cells** (11). Derive pinned FSF/SFS from any cell, detect
   conflicts, and add a pre-flight validation report for the pre-schedule
   (both roles, unavailable, inactive, late/late).
3. **Gap semantics** (12). Use exact wording in Settings, and take
   `SchedulerConfig` defaults from `SharedSettings.DEFAULTS`.
4. **`consec_violations`** (20). Count successive worked weekends.
5. **Cleanup** (21, 23). Remove `last_assignment`, move PDF export out of
   `generate_schedule`, anchor the history cutoff to `start_date`, and
   delete dead helpers.

**Done when** the pre-flight report catches each seeded bad pin, and
`last_assignment` no longer appears in `scheduler/`.

### Relation to the weekend-candidate design

`docs/weekend-candidate-generation.md` (status: pending) keeps failed
branches for manual review and forbids silent pruning. This plan is
compatible with it and moves toward it:

- Phase 1's structured result is that design's phase 1 without the
  manual-review queue.
- Phase 3's "incomplete search" flag meets its §13 requirement for any
  operational cap.
- The Phase 3 performance work is what makes its "no provisional pruning"
  rule affordable.

## Open decisions

These change what the scheduler optimizes, so they need an owner's call
before the phases that depend on them.

1. **PRN nurses** (Phase 4): never scheduled automatically (today), last
   resort for weekday gaps (recommended), or ordinary weekday staff?
2. **Rotation repeats vs. coverage** (Phase 2): is a rotation repeat always
   worse than an unfilled weekday shift? That decides whether
   `rotation_rep` becomes a hard ranking key alongside `gaps`. *Decided:
   yes. Ranking is fewest repeats, then fewest unfilled slots, then the
   weighted score.*
3. **`rot_viol` meaning** (Phase 2): distribute new repeats toward nurses
   with fewer past repeats (recommended), or keep steering weekends away from
   past victims? *Decided: distribute new repeats (recommended).*
4. **Weekend gap bound** (Phase 4): inclusive (a 28-day cadence is allowed at
   28) or exclusive (today)? Either way the label will say exactly which.
5. **Edge weekends** (Phase 1): extend the horizon automatically
   (recommended), or require ranges to start on a Monday and end on a
   Sunday? *Phase 1 implemented the recommendation, keeping already
   recorded edge weekends as they are.*
6. **Manual pattern overrides** (Phase 1): is the proposed rule (an override
   applies only when no later weekend is recorded) right, or should
   overrides carry an explicit effective date? *Phase 1 implemented the
   proposed rule; an effective date can still replace it.*
7. **Where relaxed mode may repeat** (Phase 2): anywhere, or only on
   weekends a branch cannot staff strictly? *Decided: only where needed.*
8. **Time off in fairness** (Phase 2): proportional expected shares,
   excluding low-availability nurses, or raw counts? *Decided: raw counts
   (finding 10 closed as Won't fix).*
