# Original Monolithic Scheduler Rules & Constraints (Translated to Real-World Terms)

## Source Snapshot Used

This document records the scheduling rules as they existed in the original
single-file implementation, before it was decomposed into the `scheduler/`,
`ui/`, and `cli/` packages. It is retained as an architectural decision record:
the behaviour described here is the baseline the current engine is expected to
preserve, and the characterization tests in
`tests/test_scheduler_characterization.py` pin it down in code.

The rules below are written in plain English and grouped into:

1. **Hard constraints** (must be satisfied for a candidate assignment to be accepted)
2. **Generation behavior** (how the scheduler builds options)
3. **Soft optimization / ranking** (how it picks “best” among valid schedules)

---

## 1) Scheduling Model (What the code is trying to build)

- The schedule is daily from `start_date` to `end_date`.
- Each date has two roles:
  - **main**
  - **backup**
- Friday/Saturday/Sunday are treated as “weekend” days.
- Weekend assignments are built around two pattern nurses:
  - **FSF nurse**: main on Fri + Sun, backup on Sat
  - **SFS nurse**: backup on Fri + Sun, main on Sat
- Weekdays are Monday–Thursday only.
- PRN nurses are part of personnel data but are blocked from weekend assignment logic.

---

## 2) Hard Constraints — Weekend Rules

These are enforced when building weekend nurse pairs.

### 2.1 Weekend staffing requires full 3-day availability

A nurse can be selected for a weekend pattern only if they are available on **all three days** (Fri/Sat/Sun) for that weekend.

- Missing availability or NaN is treated as unavailable.

### 2.2 PRN nurses cannot be assigned weekends

If a nurse is marked PRN, they are rejected for weekend patterns.

### 2.3 Minimum gap between worked weekends (forward and backward)

The scheduler enforces a strict Friday-to-Friday minimum spacing:

- A nurse must have **more than `weekend_gap_days`** between worked weekends.
- This check is both:
  - **Backward-looking** (history + already assigned prior weekends in this run)
  - **Forward-looking** (future assigned weekends and pre-scheduled future weekends)

So if `weekend_gap_days = 28`, a new weekend is invalid if it is 28 days or less from the previous/next worked weekend.

### 2.4 Rotation rule (strict mode)

Each nurse’s weekend pattern should alternate:

- If last pattern was FSF, next must be SFS.
- If last pattern was SFS, next must be FSF.

In strict generation, repeats are not allowed.

### 2.5 Rotation violation mode (relaxed fallback)

If strict mode yields no variants, there is a fallback path where pattern repeats may be allowed.

- In relaxed mode, repeated pattern is allowed for nurses in the allowed set.
- If the allowed set is empty in relaxed mode, it is interpreted as “all nurses allowed.”

### 2.6 FSF and SFS nurses must be different people

Weekend pair construction always requires two distinct nurses (except impossible prefilled edge prevented by pair builder).

### 2.7 Late-shift pairing rule for weekend pairs

Two late-shift nurses cannot be paired together in the same FSF/SFS weekend pair.

- Exception: if both names are fully pre-scheduled for those slots, that exact pre-scheduled late/late pair is tolerated.

### 2.8 Pre-scheduled weekend assignments are respected and can force inclusion

If weekend assignments are prefilled, those nurses are force-included into candidate sets/pairs even if normal filters would prune them.

The code also tries to infer FSF/SFS from fully prefilled weekend rows when Friday alone doesn’t explicitly define the pattern.

---

## 3) Hard Constraints — Weekday Rules (Mon–Thu)

These are enforced while filling weekday main/backup slots.

### 3.1 One nurse cannot hold both roles on the same date

For any date, main and backup must be different nurses.

### 3.2 Nurse must be available that day

- Availability false/missing/NaN means ineligible.

### 3.3 Late-shift same-day pairing block

A nurse assignment is rejected if it would create a same-day late/late main+backup combination.

- Exception: if both cells are pre-scheduled, that combination is not disturbed.

### 3.4 Min spacing between any two assignments for the same nurse

Base rule:

- Nurse must have `min_days_between_assignments` clear on both sides around a proposed assignment date.
- Conflict is checked against both main and backup existing assignments.

### 3.5 Optional one-day-gap relaxation (configurable)

If no one is eligible under base spacing, scheduler can optionally retry with relaxed spacing (`allow_one_day_weekday_gap=True`).

Important boundaries in code:

- Relaxation applies only on Mon–Thu.
- Relaxation is **not** allowed for dates inside that nurse’s immediate pre/post weekend windows.
- Practically, this reduces spacing strictness by one day (`max(1, base - 1)`).

### 3.6 Weekly caps per nurse (Mon–Thu only)

Within a Monday–Thursday week block:

- Max **1 main** assignment per nurse
- Max **2 total** assignments (main+backup) per nurse

### 3.7 Pre-weekend window restrictions

If a weekday falls in a nurse’s pre-weekend window (up to 4 days before the next worked Friday), weekday assignment is heavily limited:

- Normal weekday assignment path: only Monday allowed.
- Gap-fill-specific path: Monday and Tuesday allowed.

### 3.8 Post-weekend window restrictions

If a weekday falls in the post-weekend window (up to 6 days after prior worked Friday), only certain days/roles are allowed via config flags:

- Wednesday main allowed if `allow_post_weekend_wednesday_main=True`
- Wednesday backup allowed if `allow_post_weekend_wednesday_backup=True`
- Thursday main allowed if `allow_post_weekend_thursday_main=True`
- Thursday backup allowed if `allow_post_weekend_thursday_backup=True`
- Monday/Tuesday in this post-window are blocked by this validator.

### 3.9 Pre-scheduled weekday cells are immutable

If a date-role cell is pre-scheduled, assignment logic skips modifying it.

---

## 4) Generation Behavior (How feasible schedules are built)

### 4.1 Weekend-first branching

The scheduler builds all feasible weekend variants across Fridays in range.

- For each Friday, it generates all valid FSF/SFS pairs under constraints.
- Each pair creates a cloned variant branch.

### 4.2 Then weekdays are filled

For each weekend variant, Monday–Thursday slots are filled date-by-date, role-by-role.

- main attempted before backup on each day.
- If no eligible nurse for a slot, that slot is left empty (gap).

### 4.3 Candidate ordering for deterministic picks

When multiple eligible weekday nurses exist, tie-breaking order is:

1. fewest assignments in that role
2. fewest assignments on that same weekday (Mon/Tue/Wed/Thu fairness)
3. fewest total assignments (main+backup)
4. fewest recent historical assignments (last window)
5. canonical nurse name order (deterministic fallback)

---

## 5) Soft Objectives / Ranking (Not hard-fail constraints)

After feasible variants are created, they are ranked with weighted metrics (lower is better).

### 5.1 Metrics considered

- **rotation_rep**: number of repeated weekend patterns in the produced schedule
- **gaps**: count of empty main/backup cells (weekday gap pressure)
- **rot_viol**: penalty based on historic rotation violation counts
- **weekend_gap**: penalty from spread/deficit of Friday-to-Friday intervals across all nurses
- **balance**: main spread + backup spread (max-min load imbalance)
- **long_term**: penalty for worsening overutilization relative to prior history window

### 5.2 Weighted normalization

- Each metric normalized to 0..1 across candidate variants.
- Multiplied by configured weights.
- Summed to `weighted_score`.
- Sorted ascending.

Default weighting in this monolithic version:

- rotation_rep: 0.30
- gaps: 0.20
- rot_viol: 0.15
- weekend_gap: 0.15
- balance: 0.10
- long_term: 0.10

---

## 6) Default Policy Values in the Monolithic File

Two defaults appear in this file (settings defaults vs config constructor defaults). In real use, GUI/shared settings typically supply values.

### Shared settings defaults (used by settings loader)

- `weekend_gap_days = 28`
- `min_days_between_assignments = 2`
- `allow_post_weekend_wednesday_main = False`
- `allow_post_weekend_wednesday_backup = True`
- `allow_post_weekend_thursday_main = True`
- `allow_post_weekend_thursday_backup = True`
- `allow_one_day_weekday_gap = False`

### SchedulerConfig constructor defaults (if config passed directly)

- `weekend_gap_days = 14`
- `min_days_between_assignments = 2`
- same post-weekend defaults as above
- `allow_one_day_weekday_gap = False`

---

## 7) Practical “English Translation” of Intended Behavior

In plain operational terms, this original scheduler is trying to do the following:

1. Build weekend coverage first with two-person FSF/SFS pattern pairs.
2. Keep weekend workload separated by a minimum number of weeks/days both backward and forward.
3. Alternate weekend pattern per nurse unless rotation-violation mode is explicitly enabled.
4. For weekdays, avoid overloading any one nurse in the same week and maintain spacing between assignments.
5. Avoid late+late pairings on the same day.
6. Respect prefilled assignments as fixed commitments.
7. If no one can fit strict weekday spacing, optionally loosen by one day in limited contexts.
8. Among feasible schedules, prefer those with fewer gaps, better balance, healthier weekend spacing distribution, fewer pattern repeats, and better long-term fairness.

---

## 8) Notes / Caveats for Review

- Some methods distinguish “hard constraints” from “ranking penalties.”
  - Example: weekend spacing is both a hard eligibility gate and also appears as a soft penalty metric across all gaps.
- There are two default sources for key parameters (`SharedSettings.DEFAULTS` and `SchedulerConfig` constructor), so effective behavior depends on which path initialized config.
- Pre-scheduled data can override normal filtering via force-inclusion, so your explicit manual entries are intentionally privileged.

