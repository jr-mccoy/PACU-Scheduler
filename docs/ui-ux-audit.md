# UI/UX audit

An audit of the PySide6 desktop GUI (`ui/`), done against commit `1bb63ef`,
with the fixes that followed. Line references point at that commit. Each
finding is marked **Fixed** (in this branch) or **Deferred** (with a reason).

Severity:

- **P1**: broken, or misleading enough to cause wrong data.
- **P2**: friction or missing feedback.
- **P3**: polish.

## Method

- Read every screen, dialog, worker and theme module.
- Rendered each screen offscreen from the demo seed (`scripts/screenshots.py`).
- Drove a full generate → review → apply run and a cancelled run through the
  real GUI code.

## Conventions adopted

These are now shared helpers in `ui/widgets/common.py`, so new screens get
them for free.

- **Button hierarchy.**
  - `role="special"` (accent): the screen's one primary action, such as
    Generate, Add or Apply.
  - `role="destructive"` (red): actions that remove data.
  - Everything else, including **Back** and **Quit**, is neutral.
  - Disabled buttons now look disabled whatever their role.
- **Every screen:**
  - uses `screen_title()`, a one-line muted subtitle and a full-width neutral
    `back_button()`;
  - goes back on **Esc**;
  - reloads its data in `on_show()` whenever it is navigated to.
- **Lists and tables:**
  - an empty-state hint (`install_empty_state()`);
  - selection kept across refreshes;
  - **Enter**/double-click edits, **Delete** removes, **Ctrl+N** adds;
  - buttons that need a selection are disabled until there is one.
- **Dialogs** (`ToolDialog`):
  - **Esc** cancels and **Enter** presses the default button;
  - the title bar has a close button, which also cancels;
  - the first input gets focus.
  - Confirmations name the action ("Remove", "Apply", "Replace") instead
    of "Yes". Destructive ones are red, and default to Cancel.

## P1 — broken or misleading

| # | Finding | Where | Status |
|---|---|---|---|
| 1 | Calendar day labels are hard-coded Sunday-first, but the grid follows the locale. Every label was one column off (22 Sep 2026, a Tuesday, sat under "Mon"). | `ui/widgets/date_pickers.py:205,295` | **Fixed**: every calendar is pinned to Sunday-first. |
| 2 | The main menu showed a blank 16:9 block: `GIF.gif` is not in the repo, yet `show_gif` defaults to on. | `ui/screens/main_menu.py:37` | **Fixed**: the label hides when the movie is invalid. |
| 3 | Month arrows were invisible. The date pickers load `arrowL/R.png`, which is not shipped, and the Weekend History ◀▶ glyphs were clipped by the 16px global button padding. | `date_pickers.py:240-249,327-336`, `weekend_history_calendar.py:66-79` | **Fixed**: `nav_arrow_button()` uses the PNG when present and a large chevron otherwise, with zero padding and an accessible name. |
| 4 | `App.apply_settings` fixed the row height of *every* `QTableView`, including each calendar's internal grid, which squashed months into a strip. | `ui/app_shell.py:158-184` | **Fixed**: calendars are skipped. |
| 5 | Screens are built once and hold their own repository caches, so edits on one screen did not show on another until a restart (for example, a new nurse missing from Unavailable Dates or the weekend dialog's nurse list). | `ui/app_shell.py:90-95` | **Fixed**: `switch_frame` calls `on_show()`. The weekend→assignment copy also re-reads weekend history first. |
| 6 | Applying a schedule wrote the Friday rotations with `modify_assignment`, a SQL `UPDATE`. For weekends not yet in weekend history, which is the normal case for a new schedule, nothing was written. | `ui/dialogs/variant_review_dialog.py:220` | **Fixed**: uses `add_assignment` (insert or replace). |
| 7 | Applying a schedule had no confirmation and did not say which option was applied. | `variant_review_dialog.py:216-225` | **Fixed**: "Apply This Schedule…" confirms with the option number, the date range and what is overwritten. |
| 8 | Adding an existing nurse name silently cleared their PRN/late flags. Re-adding a removed nurse silently reactivated them. | `nurse_management.py:127`, `repositories.py:370-382` | **Fixed**: a case-insensitive duplicate check. For a removed nurse the app offers to restore them. PRN and late shift can be set when adding. |
| 9 | Assignment dialogs accepted Main == Backup and empty rows, and silently overwrote an existing date. | `prescheduled.py:101-141`, `assignment_history.py:109-147` | **Fixed**: a shared `open_assignment_dialog()` validates, and asks before replacing a day. |
| 10 | The two "Sync" buttons had mirror-image labels but ran the same one-way copy. They overwrote conflicts without asking and threw away the counts. | `assignment_history.py:217`, `weekend_history_calendar.py:277` | **Fixed**: both are labelled "Copy Weekend History → Assignment History", confirm first, and report how many days were added or replaced. |
| 11 | Six settings had no effect (details below). The compact dialog also dropped the scoring weights. | `ui/dialogs/*settings*` | **Fixed**: connected to the scheduler, or removed where no behaviour exists (see "Settings"). |

## P2 — friction and missing feedback

| # | Finding | Status |
|---|---|---|
| 12 | Back was accent-coloured on six of seven screens, outranking the primary action. Generate Schedule was a plain grey button sixth in the menu. | **Fixed**: the convention above. The main menu is grouped (Schedule / Roster / History), Generate comes first and is primary, and the column is capped at 480px. |
| 13 | No keyboard support at all. `ToolDialog` is a `QWidget`, so Esc did nothing and `setDefault` had no effect. The title bar had no close button. | **Fixed**: the conventions above; Ctrl+Q quits. |
| 14 | Generation had no cancel. The progress dialog sat at "Preparing… 0%" through weekend generation, which can take minutes, then showed `Processing n/N`. | **Fixed**: a staged label (building variants → evaluating n of N with elapsed time → saving PDFs) and a working **Cancel**. Cancel stops the pool, terminates in-flight workers, restores the history backup, and takes effect within about half a second during evaluation. |
| 15 | Errors showed the raw traceback in a fixed 340×220 box. | **Fixed**: message boxes size to their text. `show_error(details=…)` hides the traceback behind "Show details". Tracebacks also go to the log. |
| 16 | After generation, a "Schedules exported" info box and the review dialog opened at the same time, and there was no way to open the folder. | **Fixed**: the review dialog shows a banner with the folder and an **Open Folder** button. Export failures are listed, not only printed. |
| 17 | The review header read `Variant 1/5 • gaps 0 • Δmain 1 • Δbackup 1`. The score and rotation repeats that ranking uses were hidden, and nothing explained the terms. | **Fixed**: "Option n of N (best ranked)" plus labelled Score / Unfilled slots / Main spread / Backup spread / Rotation repeats, each with a tooltip. Dates show as `Mon 09/21`, with weekends highlighted. Left/Right switch options. |
| 18 | The Generate screen had no title, stacked two full-width calendars, defaulted the end date to today, and only said "End date is before start date" after a click. | **Fixed**: a title; side-by-side calendars when wide enough; a 4-week default; a live "Tue Sep 22 – Mon Oct 19, 2026 · 28 days · 4 weekends" summary; Generate disabled, with an inline reason, when the range is invalid. |
| 19 | The rotation-violation dialog used unexplained abbreviations ("Viol: 0, Streak: 0, Clean: 999"). It showed "999" for never-violated nurses, and listed inactive and PRN nurses. Its checkboxes were black-on-white in every theme, and Select All appeared only with more than 8 nurses. | **Fixed**: a plain-language intro; rows read "never violated" or "2 violations, streak 1, 3 clean weekends"; only active non-PRN nurses; theme colours; Select All and Select None always; the confirm button reads "Start Generating". |
| 20 | Rotation stats sorted numbers as text ("10" before "9"), the ascending/descending toggle never worked, and 999 was shown literally. Errors used the info icon. Back sat inside the edit group box. The title did not match the menu label. | **Fixed**: the real header with sort arrows and tooltips; numeric and date sorting; "—" / "Never"; `show_error`; a standard Back; the menu label and title are both "Rotation Violation Stats". The Rebuild confirm warns that manual overrides are replaced. |
| 21 | Unavailable Dates had no title, silently left out PRN nurses, listed past dates forever, clipped the second line of each card, and lost the selection on every keystroke. | **Fixed**: title; PRN shown with a tag; "Include past dates" (off by default); day counts; correct card heights; selection kept; "No nurse matches …" empty state. |
| 22 | Weekend History rows showed a bare "Fri Sep 04" when nothing was recorded. "Cancel" meant "clear selection". Rows with only one nurse set could be added to but not edited or removed. | **Fixed**: rows read "not recorded" or "FSF: … SFS: …" with the year; "Clear Selection"; "This Month"; PgUp/PgDn; partial rows can be edited and removed; the dialog requires two different nurses. |
| 23 | Removing a nurse said only "Remove X?". It did not say the removal is a soft delete. | **Fixed**: "They will no longer be scheduled. Past assignments and weekend history are kept." |
| 24 | Warning and error text colours were chosen for light backgrounds (about 2.6:1 on the dark theme). Weekday headers were red-on-blue (about 1.5:1). | **Fixed**: per-theme semantic colours in `UiStyle.PALETTES`, at 4.5:1 or better. Weekend headers use a darker accent shade instead of red. |

## P3 — polish

| # | Finding | Status |
|---|---|---|
| 25 | "Nurse Scheduler" in the window and menu; "PACU Scheduler" in the README. | **Fixed**: "PACU Scheduler" everywhere in the GUI. (`~/.nurse_scheduler` keeps its name so existing settings still load.) |
| 26 | Nurse status shown as `Name [Late]`; the name was recovered by parsing the text. | **Fixed**: "Name · Late shift". The name is stored in item data. |
| 27 | Inconsistent margins, button heights (36 / 40 / 48) and title fonts across screens. | **Fixed**: shared helpers. |
| 28 | Unchecked checkboxes were almost invisible on the dark window background. | **Fixed**: a visible outline. |
| 29 | Stray `print()` calls in the UI (export, settings, debug setup, the review dialog). | **Fixed**: `logging`. |
| 30 | `Roboto` is requested everywhere but not bundled, so Qt substitutes a fallback. | **Deferred**: bundling a font is a packaging decision. |
| 31 | No window or app icon. | **Deferred**: needs artwork. |

## Settings

| Setting | Before | Now |
|---|---|---|
| Show calendar grid | Ignored; grids always on | Applied to every calendar, live. |
| History duration (months) | Never passed; always 6 | Passed through `build_scheduler_from_settings` → `NurseScheduler` → `AssignmentHistory`. Shared with the CLI. |
| Measure phase times | No consumer | Runs profiled evaluation and writes `performance_metrics.json` into the run's export folder. **Default changed to off**, because it never ran before. |
| Analyse initial gaps + Gap report file | No consumer | Writes a per-option "empty slots before/after repair" report (`scheduler/exporters/gap_report.py`) into the export folder, or to an absolute path if one is given. **Default off.** |
| Midweek pair: both Backup / Main + Backup | Folded into the master flag and then ignored | Role-scoped one-day gap in `ScheduleVariant._has_sufficient_spacing`: a Mon–Wed/Tue–Thu pair is allowed only for the chosen role pairs. "Any roles" (the master flag) still allows every pair, as before. |
| Scoring weights (compact dialog) | Missing | A "Weights" tab. |
| Main score factor, Backup score factor, Availability penalty | Shown and saved, but never read by the scheduler, not even in the original monolith | **Removed from the dialogs.** There is no existing behaviour to connect them to, and inventing a scoring rule was out of scope. Saved values are left in `settings.json`. |

Both dialogs also gained:

- a tooltip on every field;
- **Restore Defaults**, which resets the form but saves nothing until you click Save;
- accent-colour validation (the desktop dialog also has a colour picker);
- pinned Save/Cancel buttons (the nested scroll area is gone).

## Deferred

- **Colour tokens.** `style.py`, `theme.py` and `date_pickers.py` still repeat
  hex values per theme. `UiStyle.PALETTES` is a start. Moving all the QSS onto
  tokens is a larger refactor with visual-regression risk.
- **Side-by-side comparison of options** in the review dialog. Paging with
  labelled metrics and the HTML calendar view covers the need for now.
- **Undo after applying a schedule.** The confirmation guards the action.
  A true undo needs the pre-apply state of assignment history, which is not
  captured.
- **PDF export off the GUI thread.** It runs after evaluation, behind a
  "Saving PDF and HTML copies…" label. Moving it to the worker changes the
  thread ownership of the scheduler object.
- **Main/Backup score factors and availability penalty.** Decide whether they
  should drive scoring. If so, they need a defined meaning first.
