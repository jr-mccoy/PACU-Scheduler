PACU-SCHEDULER — Coupling & Cohesion Analysis
1. Overall Assessment
The repository is mid-migration from two monolithic modules (scheduler/legacy_core.py — 7,772 LOC; ui/legacy.py — 5,231 LOC) toward focused, single-purpose modules. The migration is acknowledged in README.md:14-22 and code-review.md:899-906. The extracted modules (scheduler/constraints.py, scheduler/scoring.py, scheduler/assignment.py, scheduler/history_services.py, ui/theme.py) show high cohesion and low coupling, but the bulk of the logic still lives inside god-classes inside the two legacy files.
Confidence: High — based on direct file/class/method counts, import graph inspection, and an explicit in-tree migration checklist (code-review.md:899-906) and TODO (README.md:14-22) confirming intent.
Area
Status
Confidence
Extracted helpers (constraints, scoring, assignment, history_services)
Healthy
High
scheduler/legacy_core.py god-classes
Genuinely problematic
High
ui/legacy.py god-file
Genuinely problematic
High
New facade modules (domain.py, repositories.py, engine.py)
Acceptable transition; weak ownership today
High
UI → backend boundary
Genuinely leaky (imports private module)
High
Qt framework coupling
Appropriate / not a finding
High
2. Verified High-Coupling Findings
2.1 UI imports the backend's private legacy module directly
Evidence: ui/legacy.py:38
Python
plus ui/legacy.py:39-43 imports NurseScheduler, NurseSchedulerUI, WeekendHistory, _evaluate_variant_worker, etc.
The package already exposes a curated facade in scheduler/__init__.py:30-57 whose docstring (scheduler/__init__.py:1-28) explicitly says external callers "should import backend symbols from this package instead of versioned module filenames." The UI bypasses that facade and reaches into the implementation module.
Implication: Any rename/restructure inside legacy_core.py (which the migration plan calls for) silently breaks ui/legacy.py. This is the most significant cross-package coupling violation — it negates the entire facade strategy.
Suggested fix: Replace import scheduler.legacy_core as backend_mod with imports from the scheduler facade; or, if a symbol the UI needs is missing, add it to the facade rather than reaching inside.
2.2 "Extract method object" without breaking the back-reference
Evidence: scheduler/optimization/window_refill.py:6-10, scheduler/generation/domain.py:6-10, scheduler/generation/ordering.py.
Python
WindowRefillOptimizer.backtrack_window (window_refill.py:30-108) and iterative_window_refill_rebalance (window_refill.py:110-208) call ~20 private members on variant: _console_debug, _debug_print, is_empty, state.schedule, _eligible_domain, _eligible_domain_gap, _inc_assign, _dec_assign, _collect_weekday_windows, _spread_components, _lexi_better, backup_week_assignments, _clear_window_assignments, _build_window_varlist, _restore_from_backup, StateSnapshot, BestStateTracker, Comparison, config.min_days_between_assignments, etc.
ScheduleVariant.__init__ (scheduler/legacy_core.py:2662-2664) holds the inverse references:
Python
Implication: This is structural feature-envy with a circular reference. The extracts moved code geographically but did not reduce coupling — the optimizer can only function on a fully-constructed ScheduleVariant. Renaming any _ method on ScheduleVariant breaks the optimizer.
Suggested fix: Promote the methods the optimizer actually needs (e.g., _spread_components, _lexi_better, _eligible_domain) into a small, public VariantContext protocol or pass them as explicit parameters. Then WindowRefillOptimizer depends on a stable interface, not a class internals.
2.3 App constructor wires every screen by concrete class
Evidence: ui/legacy.py:5031-5046:
Python
All eight screens are constructed eagerly with parent=self, and each screen reaches back through parent.backend (the NurseSchedulerUI CLI object) to call domain operations. This is normal-for-Qt MainWindow wiring, but combined with the screens living in the same 5,231-line file, the coupling is more than necessary.
Implication: Moderate — typical Qt structure justifies some coupling. The concrete problem is not the wiring pattern but that every screen sits in ui/legacy.py and reads sibling-private state.
Suggested fix: Continue the in-progress extraction of screens into ui/screens/* (today they are thin compatibility wrappers, e.g. ui/screens/nurse_management.py:1-22).
2.4 Facade modules without ownership
Evidence: scheduler/domain.py:1-5, scheduler/repositories.py:1-8, scheduler/engine.py:1-6 all begin with:
Python
The docstring claims ownership; the body re-exports from legacy_core. code-review.md:906 explicitly tracks this debt: "Follow-up: remove temporary legacy imports once direct implementations are extracted from scheduler/legacy_core.py and ui/legacy.py."
Implication: Low — this is acknowledged transitional debt, not a design flaw. It is appropriate to flag because anyone reading these files will assume the symbols live here.
3. Verified Low-Cohesion Findings
3.1 scheduler/legacy_core.py mixes ~10 unrelated concerns
grep "^class " returns 14 top-level classes in one file (scheduler/legacy_core.py:185-7727). Among them:
Performance instrumentation: PhaseMetrics, WorkerMetrics, PerformanceProfiler, MetricsCollector, PerformanceReport (lines 185-415)
Domain config: SharedSettings, SchedulerConfig (lines 775, 2099)
Persistence: DatabaseMixin, AssignmentHistory, NurseManager, WeekendHistory (lines 901-1988)
Domain model: ScheduleState, ScheduleVariant, ScheduleQuality, BestStateTracker, StateSnapshot (lines 2182-4641)
Orchestrator: NurseScheduler (lines 4759-6186)
CLI menu loop: NurseSchedulerUI (lines 6442-7726) — 58 methods of input()/print() interaction
PDF rendering helpers, debug logger, CLI helper, validators
Implication: The file is unnavigable, breaks process model boundaries (CLI in same module as algorithms), and forces every importer to load Pandas, ReportLab, sqlite3, tqdm, optional psutil regardless of which class they want.
Confidence: High — this is the type of issue the in-tree code-review.md already targets.
3.2 NurseSchedulerUI is in the wrong package
Evidence: scheduler/legacy_core.py:6442-7726 — a 1,284-line CLI menu class. Methods include _handle_add_nurse, main_menu, _display_menu_and_get_choice, nurse_management_menu (scheduler/legacy_core.py:6498-6700). It uses input()/print() directly.
README.md:21 confirms NCSSQLGUIFINALIST57 -> ui — but NurseSchedulerUI is still imported into the GUI through the scheduler package (ui/legacy.py:42, then ui/legacy.py:5029: self.backend = NurseSchedulerUI(DB_NAME)).
Implication: This is a textbook cohesion violation: a CLI presentation layer is in the scheduler (algorithm) package, and the Qt GUI uses it as a backend service object. The two screens of the same app talk to one another via a CLI controller object — meaning the GUI inherits CLI helpers like _get_nurse_name(prompt=...) and CLIHelper.pause().
Suggested fix: Split NurseSchedulerUI into (a) a pure backend service (move to scheduler/) that owns repositories/scheduler construction, and (b) a CLI presenter (move to a new cli/ package or delete if the GUI is the sole consumer).
3.3 WeekendHistory mixes canonical and derived state
Evidence: scheduler/legacy_core.py:1492-1988 (~497 lines, 40 methods). Holds:
Canonical: _assignments (loaded from weekend_assignments table, legacy_core.py:1528-1541)
Derived: _last_patterns, weekend_rotation_history, rotation_violation_stats, rotation_violation_dates
Plus orchestration (weekend_service, violation_service).
The in-repo code-review.md:183-218 calls this out as the biggest structural fix needed and traces concrete state-corruption bugs (add_assignment, modify_assignment, restore) to the lack of a single rebuild path. Some of the prescription (_rebuild_derived_weekend_state) has been implemented (scheduler/history_services.py:75-125), but the class still owns both layers.
Implication: Low cohesion has produced an actual correctness bug class, not just code-readability harm. This is the strongest case in the repo that low cohesion is harmful here, not theoretical.
3.4 ScheduleVariant is a 2,043-line class with 84 methods
Evidence: scheduler/legacy_core.py:2598-4641. Methods span:
Mutation primitives (_inc_assign, _dec_assign, _modify_schedule)
Eligibility checks (_passes_basic_eligibility_checks, _passes_advanced_eligibility_checks, _validate_weekday_relative_to_weekend, _validate_post_weekend_assignment)
Caching (_get_total_counts, _get_index_set, _weekday_counts_for, _invalidate_weekday_cache)
Window backtracking (_backtrack_window, _eligible_domain, _build_window_varlist)
Week permutation rebalance (_rebalance_all_weeks, _try_week_permutations_no_revert, _try_week_gap_permutations_no_revert)
Gap fill (iterative_gap_fill_no_revert, _fill_week_with_permutation)
Weekend assignment (assign_weekend, _apply_weekend_assignments, _update_pattern_tracking)
Spread metrics (_spread_components, _spread_main_backup, _lexi_better, _rebalance_score)
Debug logging delegation
Some functionality has already been extracted (scheduler/constraints.py:21-109, scheduler/assignment.py:19-72, scheduler/scoring.py:30-107) and these are well-cohesive examples — but the extracts are largely duplicated: _passes_basic_eligibility_checks exists on ScheduleVariant and in scheduler/constraints.py. The variant has not been reduced.
Implication: Search/mutate/eligibility/metric concerns are interleaved, making the tracker correctness bugs called out in code-review.md:117-145 hard to localize.
3.5 NurseScheduler is a 1,427-line class with 61 methods
Evidence: scheduler/legacy_core.py:4759-6186. The class fans across initialization, historical counts, weekend variant generation, pair validation, evaluator orchestration, PDF export (_export_variant_pdf, _draw_weekday_header, _draw_week_rows), profiling reporting, and ranking.
Implication: PDF rendering does not belong on the same class as variant search orchestration. (Also explains the existence of a duplicate _evaluate_variant_worker_profiled flagged in code-review.md:436-478.)
3.6 ui/legacy.py mixes 20+ unrelated concerns
grep "^class " returns 30 top-level classes in ui/legacy.py: theming, dialogs (Settings, CompactSettings, ToolDialog, VariantReview, RotationViolation), six screens (MainMenu, NurseManagement, PreScheduled, AssignmentHistory, ViewAllUnavailable, WeekendHistoryCalendar, ScheduleGeneration, AdvancedWeekendStats), date pickers (Multi/SingleDatePicker), header views (Pink/Tall/MultiLine), the worker thread (ScheduleProgressWorker), and App itself. The ui/screens/, ui/dialogs/, ui/presenters/, ui/services/, ui/theme.py packages exist (the migration target) but most are still thin re-exports of legacy classes (e.g. ui/screens/nurse_management.py:1-24 re-uses _LegacyNurseManagementScreen).
4. Coupling/Cohesion That Is Appropriate (Not Findings)
To be precise about what is not a design flaw:
PySide6 Qt coupling. Screen classes inherit QWidget, dialogs from QDialog/QWidget. This is framework-mandated and explicitly excluded by the prompt's false-positive guard.
DatabaseMixin shared by repositories (scheduler/repositories.py:13-58, used by AssignmentHistory, NurseManager, WeekendHistory): correct mixin coupling to a small, stable interface — appropriate.
Small extracted modules are high cohesion / low coupling:
scheduler/constraints.py (109 LOC) — pure functions, depends only on pandas
scheduler/scoring.py (107 LOC) — pure dataclasses + scoring
scheduler/assignment.py (72 LOC) — pure apply/revert
scheduler/history_services.py (177 LOC) — command-style service over a single aggregate
ui/theme.py:8-28 — pure color math
These are reference implementations the rest of the codebase should look like.
scheduler/__init__.py intentionally exposing a single facade (~30 symbols) is a normal package API; the size is large only because it currently re-exports legacy CLI/UI shims that should leave the package (3.2).
Facade modules with deferred ownership (domain.py, repositories.py, engine.py) — appropriate as a transitional technique; they are listed in code-review.md:899-906 with a tracking checkbox.
5. Tradeoff-Aware Recommendations
Ordered by ratio of correctness impact to risk.
Make ui/legacy.py import from the scheduler facade only. Delete import scheduler.legacy_core as backend_mod (ui/legacy.py:38). Re-export any missing symbols from scheduler/__init__.py. Low risk; preserves the abstraction the facade was created for.
Move NurseSchedulerUI out of scheduler/. Either to a new cli/ package or deleted if obsolete. Split the constructor's "compose repositories + scheduler" responsibility into a small SchedulerFactory that both the GUI and any CLI can call. Medium risk — App.backend (ui/legacy.py:5029) currently depends on it; introduce a small protocol first.
Reduce WeekendHistory to canonical data + one rebuild orchestration (largely scoped already in scheduler/history_services.py). Per code-review.md:183-298, this is the highest-correctness-yield refactor. The infrastructure (WeekendHistoryService._run_command) is in place — the final step is removing the duplicate writers still on WeekendHistory.
Break the WindowRefillOptimizer ↔ ScheduleVariant back-reference. Define a small VariantSearchContext protocol exposing only the public surface the optimizer/builder need (assign/unassign, eligibility query, spread tuple, snapshot/restore). Then construct WindowRefillOptimizer(context) rather than WindowRefillOptimizer(variant). Tradeoff: protocol surface grows over time; counter with strict adherence to "no underscore attrs".
Extract PDF rendering off NurseScheduler. _export_variant_pdf, _draw_weekday_header, _draw_week_rows move to scheduler/exporters/pdf.py (or ui/services/variant_export.py, which already exists). Low risk; reportlab dependency moves with it.
Continue the screen/dialog extraction: the empty wrapper subclasses in ui/screens/*_screen.py and ui/dialogs/*_widget.py need to own their implementation instead of re-exporting from ui.legacy. This is the lowest-risk mechanical migration but pays off only once ui/legacy.py actually shrinks.
Deduplicate _evaluate_variant_worker / _evaluate_variant_worker_profiled. scheduler/engine.py:9-25 already defines WorkerTuningConfig; the two workers in legacy_core.py:417-657 should consume it. (Also called out in code-review.md:436-478.)
6. Summary
The codebase has two distinct populations of modules: a small, well-cohesive set of extracted helpers that follow good practice, and two large legacy files that hold the working algorithm and UI. The most actionable findings are the cross-package facade bypass (§2.1), the misplaced CLI inside scheduler/ (§3.2), the dual-state WeekendHistory (§3.3 — which is the source of real correctness bugs documented in code-review.md), and the back-reference shape of the recent "optimizer/builder" extractions (§2.2). Everything else is in-progress migration where the direction is correct and the prescription is already written in code-review.md and README.md.
