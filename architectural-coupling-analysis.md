# PACU-SCHEDULER — Coupling & Cohesion Analysis

## 1. Overall Assessment

The repository is mid-migration from two monolithic modules (`scheduler/legacy_core.py` — 7,772 LOC; `ui/legacy.py` — 5,231 LOC) toward focused, single-purpose modules. The migration is acknowledged in `README.md:14-22` and `code-review.md:899-906`. The extracted modules (`scheduler/constraints.py`, `scheduler/scoring.py`, `scheduler/assignment.py`, `scheduler/history_services.py`, `ui/theme.py`) show high cohesion and low coupling, but the bulk of the logic still lives inside god-classes inside the two legacy files.

**Confidence: High** — based on direct file/class/method counts, import graph inspection, and an explicit in-tree migration checklist (`code-review.md:899-906`) and TODO (`README.md:14-22`) confirming intent.

| Area | Status | Confidence |
| --- | --- | --- |
| Extracted helpers (constraints, scoring, assignment, history_services) | Healthy | High |
| `scheduler/legacy_core.py` god-classes | Genuinely problematic | High |
| `ui/legacy.py` god-file | Genuinely problematic | High |
| New facade modules (`domain.py`, `repositories.py`, `engine.py`) | Acceptable transition; weak ownership today | High |
| UI → backend boundary | Genuinely leaky (imports private module) | High |
| Qt framework coupling | Appropriate / not a finding | High |

---

## 2. Verified High-Coupling Findings

### 2.1 UI imports the backend's private legacy module directly

**Evidence:** `ui/legacy.py:38`

```python
import scheduler.legacy_core as backend_mod
```

plus `ui/legacy.py:39-43` imports `NurseScheduler`, `NurseSchedulerUI`, `WeekendHistory`, `_evaluate_variant_worker`, etc.

The package already exposes a curated facade in `scheduler/__init__.py:30-57` whose docstring (`scheduler/__init__.py:1-28`) explicitly says external callers *"should import backend symbols from this package instead of versioned module filenames."* The UI bypasses that facade and reaches into the implementation module.

**Implication:** Any rename/restructure inside `legacy_core.py` (which the migration plan calls for) silently breaks `ui/legacy.py`. This is the most significant cross-package coupling violation — it negates the entire facade strategy.

**Suggested fix:** Replace `import scheduler.legacy_core as backend_mod` with imports from the `scheduler` facade; or, if a symbol the UI needs is missing, add it to the facade rather than reaching inside.

### 2.2 "Extract method object" without breaking the back-reference

**Evidence:** `scheduler/optimization/window_refill.py:6-10`, `scheduler/generation/domain.py:6-10`, `scheduler/generation/ordering.py`.

```python
# WindowRefillOptimizer holds a reference back to the variant it operates on
self.variant = variant
```

`WindowRefillOptimizer.backtrack_window` (`window_refill.py:30-108`) and `iterative_window_refill_rebalance` (`window_refill.py:110-208`) call ~20 private members on `variant`: `_console_debug`, `_debug_print`, `is_empty`, `state.schedule`, `_eligible_domain`, `_eligible_domain_gap`, `_inc_assign`, `_dec_assign`, `_collect_weekday_windows`, `_spread_components`, `_lexi_better`, `backup_week_assignments`, `_clear_window_assignments`, `_build_window_varlist`, `_restore_from_backup`, `StateSnapshot`, `BestStateTracker`, `Comparison`, `config.min_days_between_assignments`, etc.

`ScheduleVariant.__init__` (`scheduler/legacy_core.py:2662-2664`) holds the inverse references:

```python
self.window_refill_optimizer = WindowRefillOptimizer(self)
# ... and similar back-references for builder/ordering helpers
```

**Implication:** This is structural feature-envy with a circular reference. The extracts moved code *geographically* but did not reduce coupling — the optimizer can only function on a fully-constructed `ScheduleVariant`. Renaming any `_` method on `ScheduleVariant` breaks the optimizer.

**Suggested fix:** Promote the methods the optimizer actually needs (e.g., `_spread_components`, `_lexi_better`, `_eligible_domain`) into a small, public `VariantContext` protocol or pass them as explicit parameters. Then `WindowRefillOptimizer` depends on a stable interface, not class internals.

### 2.3 App constructor wires every screen by concrete class

**Evidence:** `ui/legacy.py:5031-5046`:

```python
self.main_menu     = MainMenuScreen(parent=self)
self.nurse_mgmt    = NurseManagementScreen(parent=self)
self.prescheduled  = PreScheduledScreen(parent=self)
# ... eight screens total, each constructed eagerly
```

All eight screens are constructed eagerly with `parent=self`, and each screen reaches back through `parent.backend` (the `NurseSchedulerUI` CLI object) to call domain operations. This is normal-for-Qt MainWindow wiring, but combined with the screens living in the same 5,231-line file, the coupling is more than necessary.

**Implication:** Moderate — typical Qt structure justifies some coupling. The concrete problem is not the wiring pattern but that every screen sits in `ui/legacy.py` and reads sibling-private state.

**Suggested fix:** Continue the in-progress extraction of screens into `ui/screens/*` (today they are thin compatibility wrappers, e.g. `ui/screens/nurse_management.py:1-22`).

### 2.4 Facade modules without ownership

**Evidence:** `scheduler/domain.py:1-5`, `scheduler/repositories.py:1-8`, `scheduler/engine.py:1-6` all begin with:

```python
"""<<owning concept>> module.

This module currently re-exports from scheduler.legacy_core; the long-term
intent is for the implementations to live here directly.
"""
from .legacy_core import ...
```

The docstring claims ownership; the body re-exports from `legacy_core`. `code-review.md:906` explicitly tracks this debt: *"Follow-up: remove temporary legacy imports once direct implementations are extracted from `scheduler/legacy_core.py` and `ui/legacy.py`."*

**Implication:** Low — this is acknowledged transitional debt, not a design flaw. It is appropriate to flag because anyone reading these files will assume the symbols live here.

---

## 3. Verified Low-Cohesion Findings

### 3.1 `scheduler/legacy_core.py` mixes ~10 unrelated concerns

`grep "^class "` returns 14 top-level classes in one file (`scheduler/legacy_core.py:185-7727`). Among them:

- **Performance instrumentation:** `PhaseMetrics`, `WorkerMetrics`, `PerformanceProfiler`, `MetricsCollector`, `PerformanceReport` (lines 185-415)
- **Domain config:** `SharedSettings`, `SchedulerConfig` (lines 775, 2099)
- **Persistence:** `DatabaseMixin`, `AssignmentHistory`, `NurseManager`, `WeekendHistory` (lines 901-1988)
- **Domain model:** `ScheduleState`, `ScheduleVariant`, `ScheduleQuality`, `BestStateTracker`, `StateSnapshot` (lines 2182-4641)
- **Orchestrator:** `NurseScheduler` (lines 4759-6186)
- **CLI menu loop:** `NurseSchedulerUI` (lines 6442-7726) — 58 methods of `input()`/`print()` interaction
- **PDF rendering helpers**, **debug logger**, **CLI helper**, **validators**

**Implication:** The file is unnavigable, breaks process-model boundaries (CLI in same module as algorithms), and forces every importer to load Pandas, ReportLab, sqlite3, tqdm, optional psutil regardless of which class they want.

**Confidence: High** — this is the type of issue the in-tree `code-review.md` already targets.

### 3.2 `NurseSchedulerUI` is in the wrong package

**Evidence:** `scheduler/legacy_core.py:6442-7726` — a 1,284-line CLI menu class. Methods include `_handle_add_nurse`, `main_menu`, `_display_menu_and_get_choice`, `nurse_management_menu` (`scheduler/legacy_core.py:6498-6700`). It uses `input()`/`print()` directly.

`README.md:21` confirms `NCSSQLGUIFINALIST57 -> ui` — but `NurseSchedulerUI` is still imported into the GUI through the `scheduler` package (`ui/legacy.py:42`, then `ui/legacy.py:5029`: `self.backend = NurseSchedulerUI(DB_NAME)`).

**Implication:** This is a textbook cohesion violation: a CLI presentation layer is in the `scheduler` (algorithm) package, and the Qt GUI uses it as a backend service object. The two screens of the same app talk to one another via a CLI controller object — meaning the GUI inherits CLI helpers like `_get_nurse_name(prompt=...)` and `CLIHelper.pause()`.

**Suggested fix:** Split `NurseSchedulerUI` into (a) a pure backend service (move to `scheduler/`) that owns repositories/scheduler construction, and (b) a CLI presenter (move to a new `cli/` package or delete if the GUI is the sole consumer).

### 3.3 `WeekendHistory` mixes canonical and derived state

**Evidence:** `scheduler/legacy_core.py:1492-1988` (~497 lines, 40 methods). Holds:

- **Canonical:** `_assignments` (loaded from `weekend_assignments` table, `legacy_core.py:1528-1541`)
- **Derived:** `_last_patterns`, `weekend_rotation_history`, `rotation_violation_stats`, `rotation_violation_dates`
- Plus orchestration (`weekend_service`, `violation_service`).

The in-repo `code-review.md:183-218` calls this out as the biggest structural fix needed and traces concrete state-corruption bugs (`add_assignment`, `modify_assignment`, `restore`) to the lack of a single rebuild path. Some of the prescription (`_rebuild_derived_weekend_state`) has been implemented (`scheduler/history_services.py:75-125`), but the class still owns both layers.

**Implication:** Low cohesion has produced an actual correctness bug class, not just code-readability harm. This is the strongest case in the repo that low cohesion is harmful here, not theoretical.

### 3.4 `ScheduleVariant` is a 2,043-line class with 84 methods

**Evidence:** `scheduler/legacy_core.py:2598-4641`. Methods span:

- **Mutation primitives:** `_inc_assign`, `_dec_assign`, `_modify_schedule`
- **Eligibility checks:** `_passes_basic_eligibility_checks`, `_passes_advanced_eligibility_checks`, `_validate_weekday_relative_to_weekend`, `_validate_post_weekend_assignment`
- **Caching:** `_get_total_counts`, `_get_index_set`, `_weekday_counts_for`, `_invalidate_weekday_cache`
- **Window backtracking:** `_backtrack_window`, `_eligible_domain`, `_build_window_varlist`
- **Week permutation rebalance:** `_rebalance_all_weeks`, `_try_week_permutations_no_revert`, `_try_week_gap_permutations_no_revert`
- **Gap fill:** `iterative_gap_fill_no_revert`, `_fill_week_with_permutation`
- **Weekend assignment:** `assign_weekend`, `_apply_weekend_assignments`, `_update_pattern_tracking`
- **Spread metrics:** `_spread_components`, `_spread_main_backup`, `_lexi_better`, `_rebalance_score`
- **Debug logging delegation**

Some functionality has already been extracted (`scheduler/constraints.py:21-109`, `scheduler/assignment.py:19-72`, `scheduler/scoring.py:30-107`) and these are well-cohesive examples — but the extracts are largely duplicated: `_passes_basic_eligibility_checks` exists on `ScheduleVariant` and in `scheduler/constraints.py`. The variant has not been reduced.

**Implication:** Search/mutate/eligibility/metric concerns are interleaved, making the tracker correctness bugs called out in `code-review.md:117-145` hard to localize.

### 3.5 `NurseScheduler` is a 1,427-line class with 61 methods

**Evidence:** `scheduler/legacy_core.py:4759-6186`. The class fans across initialization, historical counts, weekend variant generation, pair validation, evaluator orchestration, PDF export (`_export_variant_pdf`, `_draw_weekday_header`, `_draw_week_rows`), profiling reporting, and ranking.

**Implication:** PDF rendering does not belong on the same class as variant search orchestration. (Also explains the existence of a duplicate `_evaluate_variant_worker_profiled` flagged in `code-review.md:436-478`.)

### 3.6 `ui/legacy.py` mixes 20+ unrelated concerns

`grep "^class "` returns 30 top-level classes in `ui/legacy.py`:

- **Theming**
- **Dialogs:** Settings, CompactSettings, ToolDialog, VariantReview, RotationViolation
- **Six screens:** MainMenu, NurseManagement, PreScheduled, AssignmentHistory, ViewAllUnavailable, WeekendHistoryCalendar, ScheduleGeneration, AdvancedWeekendStats
- **Date pickers:** Multi/SingleDatePicker
- **Header views:** Pink/Tall/MultiLine
- **The worker thread:** `ScheduleProgressWorker`
- **`App` itself**

The `ui/screens/`, `ui/dialogs/`, `ui/presenters/`, `ui/services/`, `ui/theme.py` packages exist (the migration target) but most are still thin re-exports of legacy classes (e.g. `ui/screens/nurse_management.py:1-24` re-uses `_LegacyNurseManagementScreen`).

---

## 4. Coupling/Cohesion That Is Appropriate (Not Findings)

To be precise about what is *not* a design flaw:

- **PySide6 Qt coupling.** Screen classes inherit `QWidget`, dialogs from `QDialog`/`QWidget`. This is framework-mandated and explicitly excluded by the prompt's false-positive guard.
- **`DatabaseMixin` shared by repositories** (`scheduler/repositories.py:13-58`, used by `AssignmentHistory`, `NurseManager`, `WeekendHistory`): correct mixin coupling to a small, stable interface — appropriate.
- **Small extracted modules are high cohesion / low coupling:**
  - `scheduler/constraints.py` (109 LOC) — pure functions, depends only on pandas
  - `scheduler/scoring.py` (107 LOC) — pure dataclasses + scoring
  - `scheduler/assignment.py` (72 LOC) — pure apply/revert
  - `scheduler/history_services.py` (177 LOC) — command-style service over a single aggregate
  - `ui/theme.py:8-28` — pure color math

  These are reference implementations the rest of the codebase should look like.
- **`scheduler/__init__.py`** intentionally exposing a single facade (~30 symbols) is a normal package API; the size is large only because it currently re-exports legacy CLI/UI shims that should leave the package (§3.2).
- **Facade modules with deferred ownership** (`domain.py`, `repositories.py`, `engine.py`) — appropriate as a transitional technique; they are listed in `code-review.md:899-906` with a tracking checkbox.

---

## 5. Tradeoff-Aware Recommendations

Ordered by ratio of correctness impact to risk.

1. **Make `ui/legacy.py` import from the scheduler facade only.** Delete `import scheduler.legacy_core as backend_mod` (`ui/legacy.py:38`). Re-export any missing symbols from `scheduler/__init__.py`. Low risk; preserves the abstraction the facade was created for.
2. **Move `NurseSchedulerUI` out of `scheduler/`.** Either to a new `cli/` package or deleted if obsolete. Split the constructor's "compose repositories + scheduler" responsibility into a small `SchedulerFactory` that both the GUI and any CLI can call. Medium risk — `App.backend` (`ui/legacy.py:5029`) currently depends on it; introduce a small protocol first.
3. **Reduce `WeekendHistory` to canonical data + one rebuild orchestration** (largely scoped already in `scheduler/history_services.py`). Per `code-review.md:183-298`, this is the highest-correctness-yield refactor. The infrastructure (`WeekendHistoryService._run_command`) is in place — the final step is removing the duplicate writers still on `WeekendHistory`.
4. **Break the `WindowRefillOptimizer` ↔ `ScheduleVariant` back-reference.** Define a small `VariantSearchContext` protocol exposing only the public surface the optimizer/builder need (assign/unassign, eligibility query, spread tuple, snapshot/restore). Then construct `WindowRefillOptimizer(context)` rather than `WindowRefillOptimizer(variant)`. Tradeoff: protocol surface grows over time; counter with strict adherence to "no underscore attrs".
5. **Extract PDF rendering off `NurseScheduler`.** `_export_variant_pdf`, `_draw_weekday_header`, `_draw_week_rows` move to `scheduler/exporters/pdf.py` (or `ui/services/variant_export.py`, which already exists). Low risk; reportlab dependency moves with it.
6. **Continue the screen/dialog extraction:** the empty wrapper subclasses in `ui/screens/*_screen.py` and `ui/dialogs/*_widget.py` need to own their implementation instead of re-exporting from `ui.legacy`. This is the lowest-risk mechanical migration but pays off only once `ui/legacy.py` actually shrinks.
7. **Deduplicate `_evaluate_variant_worker` / `_evaluate_variant_worker_profiled`.** `scheduler/engine.py:9-25` already defines `WorkerTuningConfig`; the two workers in `legacy_core.py:417-657` should consume it. (Also called out in `code-review.md:436-478`.)

---

## 6. Summary

The codebase has two distinct populations of modules: a small, well-cohesive set of extracted helpers that follow good practice, and two large legacy files that hold the working algorithm and UI. The most actionable findings are the cross-package facade bypass (§2.1), the misplaced CLI inside `scheduler/` (§3.2), the dual-state `WeekendHistory` (§3.3 — which is the source of real correctness bugs documented in `code-review.md`), and the back-reference shape of the recent "optimizer/builder" extractions (§2.2). Everything else is in-progress migration where the direction is correct and the prescription is already written in `code-review.md` and `README.md`.

---

## 7. Phased Implementation Plan

Each phase is sized to be completed in one focused session. Phases are ordered so that earlier phases harden the package boundaries the later ones rely on, and so that the highest-correctness-payoff change (Weekend history) lands once the surrounding scaffolding is stable. Within a phase, all work can be done on a single feature branch and verified independently.

### Phase 1 — Facade boundary (Recommendation #1)

**Goal:** Make `scheduler/__init__.py` the only place `ui/` touches the backend. After this phase, renaming anything inside `scheduler/legacy_core.py` cannot silently break the GUI.

**Scope**

- Replace `import scheduler.legacy_core as backend_mod` in `ui/legacy.py:38` with explicit imports from `scheduler`.
- Audit every `backend_mod.XYZ` reference in `ui/legacy.py` and `ui/**` for symbols not yet re-exported.
- For each missing symbol, add it to `scheduler/__init__.py` `__all__` and re-export from the appropriate sub-module (or mark with a `_legacy` prefix if it must stay internal).
- Remove `backend_mod` alias entirely.
- Repeat audit for `ui/app.py`, `ui/app_shell.py`, `ui/worker_threads.py`, `ui/workers.py`, `ui/services/*`, `ui/presenters/*`, `ui/screens/*`, `ui/dialogs/*`.
- Add a smoke test (or lint rule) that fails if `ui/**` imports `scheduler.legacy_core` directly.

**Out of scope**

- Splitting `legacy_core.py` internally.
- Changing which package owns `NurseSchedulerUI` (that is Phase 3).

**Verification**

- `grep -R "scheduler.legacy_core" ui/` returns nothing.
- `python -c "import ui.legacy"` succeeds.
- Existing test suite passes.
- Launch the GUI; click through the main menu, nurse management, and schedule generation flows.

**Risk:** Low. Mechanical rewiring; the package facade already exists.

---

### Phase 2 — Parallel low-risk extractions (Recommendations #5, #7)

**Goal:** Two independent mechanical extractions that shrink `legacy_core.py` and remove a duplicated worker. Both are safe to land together because they touch disjoint code.

**Scope**

- **PDF export extraction (#5):**
  - Move `_export_variant_pdf`, `_draw_weekday_header`, `_draw_week_rows` and any private helpers off `NurseScheduler` (`scheduler/legacy_core.py:4759-6186`) into a new `scheduler/exporters/pdf.py` module (function-style, taking the variant + config as parameters).
  - Replace the `NurseScheduler` methods with a single thin call site that imports the new module.
  - Move `reportlab` import out of `legacy_core.py` top-level into the new module.
- **Worker deduplication (#7):**
  - Reconcile `_evaluate_variant_worker` and `_evaluate_variant_worker_profiled` in `scheduler/legacy_core.py:417-657`.
  - Have both paths consume `scheduler/engine.py:9-25`'s `WorkerTuningConfig`.
  - Collapse to a single implementation that branches on a profiling flag, or compose via a decorator. Remove the duplicate.

**Verification**

- Existing PDF export tests pass (or, if missing, add a snapshot test that produces a known-good PDF byte count for a fixture variant).
- Worker evaluation produces identical scoring before/after for a fixed seed.
- `tests/` suite passes.
- `grep -R "_evaluate_variant_worker_profiled" .` shows only the single canonical definition.

**Risk:** Low. PDF code has no callers outside `NurseScheduler`. Worker logic is covered by integration tests.

---

### Phase 3 — Separate CLI from scheduler package (Recommendation #2)

**Goal:** Stop using a CLI presenter as the GUI's backend service object. After this phase, `scheduler/` contains zero `input()`/`print()` code paths.

**Scope**

- Introduce `scheduler/factory.py` (or extend `scheduler/engine.py`) with a `SchedulerFactory` / `build_scheduler_service(db_path)` that returns a plain composition of `NurseManager` + `PreScheduler` + `AssignmentHistory` + `WeekendHistory` + `NurseScheduler` — i.e. the half of `NurseSchedulerUI.__init__` that is not CLI-related.
- Define a `BackendService` protocol exposing only the methods the GUI actually calls on `self.backend` today (audit `ui/legacy.py:5029` callers).
- Update `App.__init__` (`ui/legacy.py:5029`) to use `SchedulerFactory` instead of constructing `NurseSchedulerUI(DB_NAME)`.
- Move `NurseSchedulerUI` and `CLIHelper` to a new top-level `cli/` package (`cli/__init__.py`, `cli/nurse_scheduler_ui.py`) or, if confirmed obsolete, delete it.
- Remove `NurseSchedulerUI` from `scheduler/__init__.py`'s `__all__` (deprecate via a shim that emits `DeprecationWarning` for one release if external callers exist).

**Verification**

- GUI launches and exercises every screen previously reachable through `self.backend`.
- `grep -R "input(" scheduler/` returns nothing.
- `grep -R "NurseSchedulerUI" ui/` returns nothing.
- If the CLI is preserved: `python -m cli` boots the menu loop.

**Risk:** Medium. The GUI's reliance on `self.backend.<cli_helper>` is implicit in many places. The protocol audit step in Phase 3 should be done before any move, and Phase 1 must already be complete (so the audit happens against facade imports, not `backend_mod`).

---

### Phase 4 — `WeekendHistory` canonical/derived split (Recommendation #3)

**Goal:** Highest-correctness payoff. Eliminate the dual-writer state-corruption class documented in `code-review.md:183-298`.

**Scope**

- Inventory all writes to `_assignments`, `_last_patterns`, `weekend_rotation_history`, `rotation_violation_stats`, `rotation_violation_dates` inside `WeekendHistory` (`scheduler/legacy_core.py:1492-1988`).
- Remove direct writes to *derived* state from `WeekendHistory.add_assignment`, `modify_assignment`, `restore`, etc. — all derived state must flow through `_rebuild_derived_weekend_state` (already implemented in `scheduler/history_services.py:75-125`).
- Make `WeekendHistory` the sole canonical store; route every mutation through `WeekendHistoryService._run_command` so the rebuild is automatic and transactional.
- Where `ViolationHistoryService` exists, do the same for the violation tables.
- Add regression tests reproducing the bug classes from `code-review.md:183-298` (add → modify → query; restore round-trip).

**Verification**

- All new regression tests pass.
- `grep -n "self\._last_patterns\s*=" scheduler/legacy_core.py` and similar derived-state writes appear only inside `_rebuild_derived_weekend_state`.
- Full test suite passes.
- Manual GUI run: generate schedule → edit assignments → reopen weekend history calendar; verify stats agree with raw assignments table.

**Risk:** Medium-high. The bug surface is real, and tests are the safety net. Do not start this phase until Phase 1 and 3 are merged so the public API of `WeekendHistory` is the only call site we have to chase.

---

### Phase 5 — `VariantSearchContext` protocol (Recommendation #4)

**Goal:** Break the `WindowRefillOptimizer` ↔ `ScheduleVariant` circular feature-envy so the optimizer/builder/ordering modules in `scheduler/optimization/` and `scheduler/generation/` depend on a stable public interface.

**Scope**

- Catalogue every attribute/method `WindowRefillOptimizer` (`scheduler/optimization/window_refill.py`), `scheduler/generation/domain.py`, and `scheduler/generation/ordering.py` access on `variant` (the doc lists ~20).
- Define `scheduler/generation/context.py` with a `VariantSearchContext` `Protocol` exposing the minimum public surface: `assign(...)`, `unassign(...)`, `eligible_domain(...)`, `spread_components()`, `snapshot()`, `restore(snapshot)`, plus typed read-only views (`state`, `config`).
- Promote the private `_` methods the protocol exposes to public names on `ScheduleVariant` (or wrap them with public-named methods that delegate). Keep underscored versions as one-line aliases temporarily; mark for removal.
- Change `WindowRefillOptimizer.__init__` to accept `context: VariantSearchContext` instead of `variant: ScheduleVariant`.
- Remove the back-reference assignment in `ScheduleVariant.__init__` (`scheduler/legacy_core.py:2662-2664`) — construct optimizer lazily from the variant itself via the protocol.
- Repeat for builder/ordering helpers if they currently hold inverse references.

**Verification**

- Type-check the new protocol with `mypy`/`pyright` against `ScheduleVariant`.
- Add a unit test that constructs `WindowRefillOptimizer` with a minimal fake context (proves no implicit `ScheduleVariant` coupling).
- Full search-quality regression: run schedule generation on a fixture set; scores must match pre-refactor baseline.

**Risk:** Medium. The optimizer is hot-path code; behavioral regressions are easy to introduce. Land Phase 5 only after Phases 1–4 because the protocol surface depends on `WeekendHistory` already being well-defined.

---

### Phase 6 — UI screens/dialogs ownership (Recommendation #6)

**Goal:** Shrink `ui/legacy.py` by moving each screen/dialog implementation into its already-existing wrapper in `ui/screens/` and `ui/dialogs/`. Mechanical and incremental — can be split across multiple sessions per screen if needed.

**Scope**

- For each `_LegacyXxxScreen` re-export in `ui/screens/*.py`, move the class body from `ui/legacy.py` into the wrapper file, drop the `_Legacy` alias, and update `ui/legacy.py` to re-export from the new home (for backward compatibility during the migration).
- Same for `ui/dialogs/*.py` (Settings, CompactSettings, ToolDialog, VariantReview, RotationViolation).
- Move date pickers and header views to `ui/widgets/` (create if absent).
- Move `ScheduleProgressWorker` from `ui/legacy.py` to `ui/worker_threads.py` (which already exists for this purpose).
- Update `App` (`ui/legacy.py:5031-5046`) to import from the new homes.
- Once a class is fully migrated, remove it from `ui/legacy.py`.

**Verification**

- After each screen migration: GUI launches; the migrated screen renders identically and all interactions work.
- `wc -l ui/legacy.py` shrinks monotonically each session.
- Final acceptance: `ui/legacy.py` becomes a deprecated compatibility shim (or is deleted entirely).

**Risk:** Low per screen, but cumulative tedium. Best done in small batches; commit per screen.

---

## 8. Implementation Tracker

Status legend: ⬜ Not started · 🟡 In progress · ✅ Complete · ⏸ Blocked

| Phase | Recommendation | Status | Branch / PR | Notes |
| ---: | --- | :---: | --- | --- |
| 1 | #1 — Facade boundary (`ui/legacy.py` → `scheduler` facade only) | ✅ | `claude/implement-phase-1-qa7d0` | Added `configure_pair_variant_debug` / `configure_assignment_debug_logger` to facade; UI now imports `scheduler` only |
| 2a | #5 — Extract PDF rendering off `NurseScheduler` | ⬜ | | Parallel-safe with 2b |
| 2b | #7 — Deduplicate `_evaluate_variant_worker[_profiled]` | ⬜ | | Parallel-safe with 2a |
| 3 | #2 — Move `NurseSchedulerUI` out of `scheduler/`; introduce `SchedulerFactory` | ⬜ | | Requires Phase 1 |
| 4 | #3 — Collapse `WeekendHistory` canonical/derived dual-writer | ⬜ | | Requires Phases 1, 3 |
| 5 | #4 — Define `VariantSearchContext`; remove optimizer back-reference | ⬜ | | Requires Phases 1–4 |
| 6 | #6 — Migrate screens/dialogs from `ui/legacy.py` into existing wrappers | ⬜ | | Incremental; can run alongside later phases |

### Per-phase sub-checklist

#### Phase 1 — Facade boundary
- [x] Remove `import scheduler.legacy_core as backend_mod` from `ui/legacy.py:38`
- [x] Audit `ui/**` for any `scheduler.legacy_core` imports (only offender was `ui/legacy.py:38`)
- [x] Add missing re-exports to `scheduler/__init__.py` (`configure_pair_variant_debug`, `configure_assignment_debug_logger` in `scheduler/debug.py`; NCSSQL55/57 shims synced)
- [x] Add lint rule / smoke test forbidding `scheduler.legacy_core` imports from `ui/**` (`tests/test_ui_import_smoke.py::test_ui_modules_do_not_import_scheduler_legacy_core_directly`)
- [ ] Manual GUI smoke test (sandboxed env has no display; verified `import ui.legacy` + `apply_backend_debug_preferences` round-trip via headless Python)

#### Phase 2a — PDF extraction
- [ ] Create `scheduler/exporters/__init__.py` and `scheduler/exporters/pdf.py`
- [ ] Move `_export_variant_pdf`, `_draw_weekday_header`, `_draw_week_rows`
- [ ] Move top-level `reportlab` import out of `legacy_core.py`
- [ ] Replace `NurseScheduler` methods with thin delegations
- [ ] PDF snapshot test for a fixture variant

#### Phase 2b — Worker dedup
- [ ] Audit differences between `_evaluate_variant_worker` and `_evaluate_variant_worker_profiled`
- [ ] Route both through `WorkerTuningConfig` (`scheduler/engine.py:9-25`)
- [ ] Collapse to single implementation
- [ ] Verify scoring parity on a fixed-seed run

#### Phase 3 — CLI separation
- [ ] Inventory `App.backend.*` call sites in `ui/`
- [ ] Define `BackendService` protocol
- [ ] Add `SchedulerFactory` / `build_scheduler_service(db_path)`
- [ ] Update `App.__init__` to use the factory
- [ ] Move `NurseSchedulerUI` + `CLIHelper` to `cli/` (or delete)
- [ ] Remove from `scheduler/__init__.py` `__all__`
- [ ] `grep -R "input(" scheduler/` returns empty

#### Phase 4 — `WeekendHistory` rebuild
- [ ] Inventory all derived-state writes in `WeekendHistory`
- [ ] Route mutations through `WeekendHistoryService._run_command`
- [ ] Remove direct derived-state writes from public mutators
- [ ] Same treatment for `ViolationHistoryService`
- [ ] Regression tests reproducing `code-review.md:183-298` bug classes
- [ ] Manual GUI: generate → edit → re-open calendar parity check

#### Phase 5 — `VariantSearchContext`
- [ ] Catalogue optimizer/builder/ordering accesses on `variant`
- [ ] Define `VariantSearchContext` Protocol in `scheduler/generation/context.py`
- [ ] Promote required underscored methods to public names
- [ ] Switch `WindowRefillOptimizer` to take `context` instead of `variant`
- [ ] Remove back-references in `ScheduleVariant.__init__`
- [ ] Type-check protocol conformance
- [ ] Score-parity regression test on fixture seeds

#### Phase 6 — UI ownership migration
- [ ] `MainMenuScreen`
- [ ] `NurseManagementScreen`
- [ ] `PreScheduledScreen`
- [ ] `AssignmentHistoryScreen`
- [ ] `ViewAllUnavailableScreen`
- [ ] `WeekendHistoryCalendarScreen`
- [ ] `ScheduleGenerationScreen`
- [ ] `AdvancedWeekendStatsScreen`
- [ ] Dialogs: `Settings`, `CompactSettings`, `ToolDialog`, `VariantReview`, `RotationViolation`
- [ ] Date pickers and header views → `ui/widgets/`
- [ ] `ScheduleProgressWorker` → `ui/worker_threads.py`
- [ ] `ui/legacy.py` reduced to compatibility shim (or deleted)
