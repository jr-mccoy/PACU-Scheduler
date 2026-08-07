# PACU Scheduler

A desktop and terminal application that builds on-call schedules for a
post-anesthesia care unit (PACU) nursing team.

Call scheduling in a small unit is a constraint-satisfaction problem with a
fairness objective layered on top. Each day needs a **Main** and a **Backup**
nurse. Weekends run in one of two rotation patterns — `FSF` (Friday/Sunday) and
`SFS` (Saturday) — and a nurse who worked one pattern is expected to alternate
to the other next time. On top of that, the scheduler has to respect
time-off requests, minimum spacing between assignments, PRN (as-needed) and
late-shift eligibility, and any assignments a manager has pinned in advance —
while spreading Main and Backup duty evenly across the team.

This project generates candidate schedules, scores them, and presents the best
options for a manager to review and approve.

## How it works

Generation runs in two stages, because weekends are the scarce resource and
constrain everything else:

1. **Weekend generation.** The scheduler enumerates valid `FSF`/`SFS` pairs for
   each weekend in the horizon, branching on every complete pair. Because the
   variant count grows roughly as `(valid pairs) ^ (weekends)`, the pool is
   pruned after each weekend to `max_weekend_variants` (default 500), keeping
   the variants with the fewest rotation repeats, the most even weekend spread,
   and the largest minimum weekend gap.
2. **Weekday completion and rebalancing.** Each surviving weekend variant is
   filled in across weekdays, then run through an iterative window-refill
   rebalance pass that evens out Main/Backup counts without violating the hard
   constraints.

Surviving variants are scored and ranked, and the top candidates are surfaced
in a review dialog.

## Architecture

The application was originally a pair of single-file monoliths and has since
been decomposed into three packages with a one-way dependency flow
(`cli`/`ui` → `scheduler`):

| Package | Responsibility |
| --- | --- |
| `scheduler/` | Scheduling state, persistence, constraints, evaluation, optimization, ranking, diagnostics, and PDF export. No UI imports. |
| `ui/` | Qt application shell, themes, settings, widgets, dialogs, screens, presenters, and export services. |
| `cli/` | Terminal input and menu workflows. |

`scheduler/legacy_core.py` and `ui/legacy.py` remain as import-compatibility
facades; neither contains application logic, and a test enforces that.

## Setup

Requires Python 3.11+.

```bash
python -m pip install -r requirements.txt
```

Launch the desktop GUI:

```bash
python main.py
```

Or the terminal UI:

```bash
python -m cli
```

On first run the application creates `nurse_schedule.db` in the working
directory from `scheduler/schema.sql`. The database starts empty — add your
team through **Nurse Management** before generating a schedule. No database is
checked into this repository, and `.gitignore` excludes `*.db` so scheduling
data (which contains real staff names and time-off records) stays local.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

The GUI tests need a Qt platform plugin. On a headless machine:

```bash
QT_QPA_PLATFORM=offscreen pytest -q
```

## Scheduling policies

The scheduler applies these policies deliberately; each one trades automation
for operator control.

- **Weekend history is canonical.** Any weekend assignment edit
  (`add_assignment`, `modify_assignment`, `remove_assignment`, `restore`)
  rewrites derived state — `weekend_rotation_history`, violation dates and
  stats, in-memory last patterns — from `weekend_assignments`, so assignments,
  patterns, and violations cannot drift apart.
- **Manual violation overrides are temporary by design.**
  `set_violation_count` writes an operator override directly, and it stands
  until an explicit rebuild (`_recalculate_violation_counts` / "Rebuild
  Violation History").
- **Weekend generation defaults to strict-then-relaxed.** Strict alternation is
  attempted across the whole horizon first. If no variants survive, the relaxed
  retry runs only after `confirm_rotation_callback` approves it — and the
  default callback declines. `STRICT_ONLY` never introduces rotation repeats;
  an infeasible weekend fails generation rather than silently relaxing.
  `RELAXED_ALLOWED` skips strict generation entirely.
- **Rotation violations are attributed per candidate schedule.** Each
  `ScheduleVariant` records the pattern repeats introduced on its own branch, and
  `get_rotation_violation_history()` is rebuilt and deduplicated from the
  surviving variants, so pruned branches and shared ancestry do not inflate
  counts.
- **Pre-scheduled weekend conflicts are hard-blocking.** Candidate `FSF`/`SFS`
  pairs are rejected outright when they contradict a non-empty prefilled
  Friday, Saturday, or Sunday cell.
- **Pre-window history counts toward weekday spacing.** Days worked in the
  `min_days_between_assignments` window immediately before the schedule start —
  drawn from weekend history and the `schedule_history` table — are seeded into
  each snapshot, so spacing is enforced across the window boundary rather than
  only inside it.

## Documentation

- [`docs/weekend-candidate-generation.md`](docs/weekend-candidate-generation.md)
  — design spec for exhaustive weekend candidate generation and the manual
  exception workflow.
- [`docs/original_monolithic_scheduler_rules.md`](docs/original_monolithic_scheduler_rules.md)
  — the pre-refactor rules, kept as an architectural decision record and pinned
  by characterization tests.

## License

[MIT](LICENSE)
