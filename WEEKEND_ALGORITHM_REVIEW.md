# Weekend Scheduling Algorithm — Code Review

> **Scope:** Review of the weekend candidate-generation code against
> `WEEKEND_CANDIDATE_GENERATION_AND_MANUAL_EXCEPTION_WORKFLOW.md`, plus an
> independent hunt for logic errors in the existing algorithm.
> **Primary files reviewed:** `scheduler/engine.py`, `scheduler/domain.py`,
> `scheduler/repositories.py`, `ui/worker_threads.py`,
> `tests/test_scheduler_characterization.py`.

## Bottom line

The specification is a design document for an as-yet-unbuilt workflow (it is
marked *"implementation pending"*), and that is what the code reflects: the
**manual-exception workflow described in §2 and §7–§20 is not implemented.**
The generator still does the exact thing the spec exists to eliminate —
`no complete pair → branch disappears`.

Separately, while reviewing the existing weekend algorithm I found several
genuine logic errors, the most serious of which is that **"strict" mode is not
actually strict.**

---

## Part A — Spec conformance: what is missing

On any weekend with no surviving pair, generation returns `[]` and the failed
branch is discarded:

- `scheduler/engine.py:978-980` — `if not variants: … return []`
- `scheduler/engine.py:989` — the blanket `except` path also returns `[]`
- `ui/worker_threads.py:137-139` — the UI mirrors this: empty → `finished.emit([], …)`

None of the spec's core machinery exists in the tree (grep-confirmed — the
identifiers appear only in the spec and `code-review.md`):

- **§7 structured result** `WeekendGenerationResult` — absent; generation
  returns a bare `list[ScheduleVariant]`.
- **§8 state objects** `ManualReviewCandidate`, `WeekendException`,
  `WeekendExceptionType`, `CandidateStatus`, `AssignmentSource` — none exist.
- **§5 outcome taxonomy** — `_get_valid_nurse_pairs` (`engine.py:761`) returns
  only the pair list. It internally computes `valid_fsf`/`valid_sfs`
  (`_get_valid_nurses_for_patterns`) but **discards the domains and the
  rejection reasons** — they only reach a debug file via `_reject`/`_dbg_pairs`.
  So FSF-only / SFS-only / both-empty / nonempty-but-no-pair (§5.2–§5.5) cannot
  be told apart.
- **§5.6 / §16 hard contradictions** — swallowed by the blanket `except` at
  `engine.py:985` and collapsed into the same silent `[]` as an ordinary
  infeasible weekend. No diagnostics, no `LOCKED_DATA_CONFLICT` distinction.
- **§4.6 / §8.5 / §10** — no assignment provenance, no manual-assignment
  locking, no resume API (`weekend_resume.py`), no exception/presenter modules
  (§14.2).

### Invariants the existing code *does* satisfy (preserve these through any refactor)

- **§4.2 candidate isolation** — `ScheduleState.clone()` (`domain.py:597`)
  deep-copies every mutable member; read-only data is shared via `_skip_copy`.
- **§4.3 immediate state updates** — `assign_weekend` (`domain.py:937`) updates
  cells, weekend tracking, per-nurse Friday lists, patterns, and counts together.
- **§4.4 no provisional fairness pruning** — generation enumerates every valid
  pair with no fairness-based cut.
- **§3.1 pattern mapping** — the FSF/SFS → cell mapping in
  `_pair_matches_prefilled_weekend_cells` (`engine.py:822`) is consistent with
  `_get_weekend_assignments` (`domain.py:997`).

The algorithm's isolation and state-update guarantees are sound; it is the
*failure-handling* half of the spec that is absent.

---

## Part B — Logic errors in the existing code

### B1. `STRICT_ONLY` is not strict; the rotation-violation user-gate is dead *(most serious)*

In `_process_weekend_variants` (`engine.py:1006-1016`) the relaxed pass fires
whenever the strict pass yields nothing *within the same call*:

```python
if not allow_rotation_violations:
    next_vars = self._generate_strict_variants(...)          # strict
if allow_rotation_violations or not next_vars:
    next_vars = self._generate_relaxed_variants(...)         # relaxes anyway
```

The characterization test `test_generation_mode_strict_then_relaxed`
(`tests/test_scheduler_characterization.py:418-435`) pins this: strict `[]`
→ relaxed runs, `calls == ["strict", "relaxed"]`.

Consequences:

- `generate_all_weekend_variants(allow_rotation_violations=False)` — used by
  **`WeekendVariantMode.STRICT_ONLY`** (`engine.py:1262-1263`) — silently emits
  rotation-repeating variants on any hard weekend. The mode's contract is
  violated.
- In `STRICT_THEN_RELAXED`, the `confirm_rotation_callback` gate
  (`engine.py:1276-1283`) is effectively unreachable: the first "strict" call
  already exhausts the relaxed fallback, so if it returns `[]`, the
  post-confirmation retry with `allow_rotation_violations=True` runs the *same*
  relaxed logic and also returns `[]`. The manager is never meaningfully asked
  before repeats are introduced.

### B2. Relaxation is population-gated, not per-branch

Same method: the strict/relaxed choice is made for the *whole variant
population* per weekend. If even one sibling survives strictly, `next_vars` is
non-empty and the relaxed pass is skipped — so any branch that could only
continue via a legitimate relaxation is dropped just because a sibling
succeeded. Under the spec those are exactly the branches that should become
manual-review candidates rather than vanishing.

### B3. Rotation-violation reporting double-counts across the search tree

`_generate_relaxed_variants` calls `_track_rotation_violations(var, …)` once per
*(parent variant × candidate pair)* **before** cloning
(`engine.py:1050-1063`), appending to the scheduler-level
`rotation_violation_history` / `_rotation_violations`. These counts therefore
aggregate over the entire branching population and are not attributable to any
single final schedule, so `get_rotation_violation_history()` is inflated and
unreliable as audit data (relevant to §8.5/§16, which want per-candidate
provenance).

### B4. Dead, divergent gap-validation code

`_is_nurse_valid_for_pattern` / `_is_nurse_valid_for_fsf` /
`_is_nurse_valid_for_sfs` (`engine.py:707-755`) are never called
(grep-confirmed). They implement a *different* backward-gap check — via
`nurse_weekend_lists` / `_get_neighboring_fridays` — than the live path
`_check_weekend_gap_constraints` (`engine.py:617`), which uses weekend history
plus a schedule scan. Two competing definitions of the same rule is a latent
correctness trap if anyone wires the unused one in.

Related: `_get_neighboring_fridays` (`domain.py:1680-1684`) uses `bisect_left`,
so when `current_date` is itself in the list, `next_fri` returns `current_date`.
This is harmless for its live weekday-window use, but would be wrong if reused
for weekend gap logic.

---

## Recommendation

- **Part A** is a large, multi-phase build. The spec itself (§19) lists policy
  questions — which rules are waivable, whether overrides incur ranking
  penalties, reinforcement-staff sourcing, persistence scope — that it
  explicitly says a coding agent should **not** decide unilaterally. Phase 1
  (§20) is the natural starting point once those decisions are made.
- **Part B** bugs are self-contained and fixable now. **B1** in particular
  (make `STRICT_ONLY` truly strict and restore the rotation gate) is a small,
  high-value change; it will require updating
  `test_generation_mode_strict_then_relaxed`, which currently encodes the buggy
  behavior.
