"""Schedule orchestration, weekend variant generation, ranking, and export."""

from __future__ import annotations

import calendar
import logging
import os
import sys
import traceback
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import timedelta
from enum import Enum
from itertools import pairwise

import numpy as np
import pandas as pd
from tqdm import tqdm

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional in minimal environments
    psutil = None

from . import runtime as _runtime
from .debug import _dbg_pairs, _dbg_variants, _pair, _reject
from .domain import (
    BestStateTracker,
    SchedulerConfig,
    ScheduleState,
    ScheduleVariant,
    WeekendPattern,
)
from .evaluation import worker as _worker
from .evaluation.config import WORKER_TUNING, WorkerTuningConfig
from .exporters.pdf import (
    draw_week_rows as _draw_week_rows_fn,
)
from .exporters.pdf import (
    draw_weekday_header as _draw_weekday_header_fn,
)
from .exporters.pdf import (
    export_variant_pdf as _export_variant_pdf_fn,
)
from .platform import allow_sleep, default_worker_count, inhibit_sleep, usable_cpu_count
from .profiling import PerformanceReport, WorkerMetrics
from .repositories import AssignmentHistory, DateUtils
from .runtime import is_empty
from .scoring import weighted_scores_from_rows

logger = logging.getLogger(__name__)
MEASURE_PHASE_TIMES = True
PERFORMANCE_PROFILING_REQUESTED = _runtime.PERFORMANCE_PROFILING_REQUESTED
PERFORMANCE_PROFILE_JSON_DEFAULT = _runtime.PERFORMANCE_PROFILE_JSON_DEFAULT


def whole_weekend_range(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    is_recorded: Callable[[pd.Timestamp], bool] = lambda friday: False,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Widen ``[start, end]`` so it does not cut a weekend in two.

    A start on Saturday or Sunday moves back to that weekend's Friday, and an
    end on Friday or Saturday moves on to its Sunday, so the FSF/SFS pair is
    generated for all three days. A weekend that ``is_recorded`` already
    belongs to the period that recorded it, so the range is left as is there
    and the recorded days inside it are kept instead.
    """
    start = DateUtils.normalize_date(start)
    end = DateUtils.normalize_date(end)
    if start.weekday() in (5, 6):
        friday = start - timedelta(days=start.weekday() - 4)
        if not is_recorded(friday):
            start = friday
    if end.weekday() in (4, 5):
        friday = end - timedelta(days=end.weekday() - 4)
        if not is_recorded(friday) and friday >= start:
            end = friday + timedelta(days=2)
    return start, end


def _sync_worker_compatibility_overrides() -> None:
    """Honor monkeypatches made through the deprecated legacy module."""
    legacy = sys.modules.get("scheduler.legacy_core")
    tracker_cls = getattr(legacy, "BestStateTracker", BestStateTracker)
    tuning = getattr(legacy, "WORKER_TUNING", WORKER_TUNING)
    measure = getattr(legacy, "MEASURE_PHASE_TIMES", MEASURE_PHASE_TIMES)
    _worker.BestStateTracker = tracker_cls
    _worker.WORKER_TUNING = tuning
    _worker.MEASURE_PHASE_TIMES = bool(measure)


def _evaluate_variant_core(args, *, with_profiling: bool):
    _sync_worker_compatibility_overrides()
    return _worker._evaluate_variant_core(args, with_profiling=with_profiling)


def _evaluate_variant_worker(args):
    return _evaluate_variant_core(args, with_profiling=False)


def _evaluate_variant_worker_profiled(args):
    return _evaluate_variant_core(args, with_profiling=True)


class NurseScheduler:
    """
    Advanced nurse scheduling system with weekend rotation management,
    constraint satisfaction, and multi-criteria optimization.
    """

    # Class constants
    WEEKDAYS = ["Friday", "Saturday", "Sunday"]
    WEEKEND_DAYS_COUNT = 3
    FRIDAY_WEEKDAY = 4

    # PDF layout constants
    PDF_FONT_SIZES = {"title": 32, "dow": 16, "dayno": 14, "name": 18}

    def __init__(
        self,
        start_date,
        end_date,
        nurses: list,
        prn_nurses: list,
        nurse_manager,
        weekend_history,
        pre_scheduler,
        config: SchedulerConfig | None = None,
        history_window_days: int = 30,
        worker_tuning: WorkerTuningConfig | None = None,
        history_duration_months: int = 6,
    ):

        # Search budgets for the evaluation phase. Sent to the workers with each
        # work item rather than read from the module global, so a caller that
        # tunes them actually reaches the worker processes.
        self.worker_tuning = worker_tuning if worker_tuning is not None else WORKER_TUNING

        # Core date and personnel setup
        self._initialize_core_attributes(
            start_date,
            end_date,
            nurses,
            prn_nurses,
            nurse_manager,
            weekend_history,
            pre_scheduler,
            config,
        )

        # Initialize scheduling data structures
        self._initialize_scheduling_data()

        # Setup historical data and tracking.  history_duration_months bounds
        # how much assignment history is loaded at all; history_window_days
        # is the fairness window inside it that ends the day before start.
        self.history_window_days = history_window_days
        self.history_duration_months = history_duration_months
        self._initialize_historical_data()

        # Initialize nurse assignment tracking
        self._initialize_nurse_tracking()

        # Configuration for rotation violations
        self.nurses_allowed_rotation_violation: set[str] = set()

    def _initialize_core_attributes(
        self,
        start_date,
        end_date,
        nurses,
        prn_nurses,
        nurse_manager,
        weekend_history,
        pre_scheduler,
        config,
    ):
        """Initialize the core attributes of the scheduler (now with deterministic nurse order)."""
        # Deterministic ordering across processes/runs
        self.nurses = sorted(list(nurses), key=str.casefold)
        self.prn_nurses = sorted(list(prn_nurses), key=str.casefold)

        self.nurse_manager = nurse_manager
        self.weekend_history = weekend_history
        self.pre_scheduler = pre_scheduler
        self.config = config if config is not None else SchedulerConfig()

        # A weekend is one FSF/SFS unit, so a range that cuts one is widened to
        # cover it whole, unless that weekend is already recorded (then its
        # days inside the range are kept as they are; see
        # _recorded_edge_weekend_slots).
        self.requested_start_date = DateUtils.normalize_date(start_date)
        self.requested_end_date = DateUtils.normalize_date(end_date)
        self.start_date, self.end_date = whole_weekend_range(
            self.requested_start_date,
            self.requested_end_date,
            is_recorded=lambda friday: self._recorded_weekend(friday) is not None,
        )

    def _initialize_scheduling_data(self):
        """Initialize the main scheduling data structures."""
        self.schedule = self._initialize_schedule()
        self.availability = self._initialize_availability()
        self.main_assignment_counts = pd.Series(0, index=self.nurses)
        self.backup_assignment_counts = pd.Series(0, index=self.nurses)

    def _initialize_nurse_tracking(self):
        """Initialize tracking data for nurse assignments and patterns."""
        self.weekend_tracking = {}
        self.last_assignment = {}
        self.last_pattern = {}
        self.rotation_violation_history = defaultdict(list)
        self._rotation_violations = []

        for nurse in self.nurses:
            last_wk = self.weekend_history.get_last_weekend_before(nurse, self.start_date)
            self.last_assignment[nurse] = last_wk
            self.last_pattern[nurse] = self._last_pattern_before_window(nurse)

    def _last_pattern_before_window(self, nurse: str) -> WeekendPattern | None:
        """The nurse's rotation pattern going into ``start_date``.

        Weekends already recorded inside the window belong to the schedule
        being replaced, so rotation alternates against the last weekend
        before the window. History objects without the window-relative query
        (test doubles, older integrations) fall back to the stored pattern.
        """
        before = getattr(self.weekend_history, "get_last_pattern_before", None)
        if before is None:
            return self.weekend_history.get_last_pattern(nurse)
        return before(nurse, self.start_date)

    def _initialize_historical_data(self):
        """Initialize historical assignment tracking."""
        try:
            self.assignment_history = AssignmentHistory(
                self.nurse_manager.db_name,
                history_duration_months=getattr(self, "history_duration_months", 6),
            )
        except Exception:
            # DB is missing or table empty – proceed with empty history
            self.assignment_history = None

        self._historical_main, self._historical_backup = self._compute_historical_counts()

    # =====================================================================
    # HISTORICAL DATA METHODS
    # =====================================================================

    def _compute_historical_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """
        Return two dictionaries:
            • {nurse: main_count}
            • {nurse: backup_count}

        covering the previous `history_window_days` (default 30) that END
        the day *before* `self.start_date`.

        If the history table is empty or missing, we return 0-filled dicts
        so the scheduler continues without long-term logic.
        """
        empty = {n: 0 for n in self.nurses}

        if not getattr(self, "assignment_history", None):
            return empty.copy(), empty.copy()

        window_start = (self.start_date - timedelta(days=self.history_window_days)).normalize()
        window_end = (self.start_date - timedelta(days=1)).normalize()

        try:
            main, backup = self.assignment_history.get_counts(window_start, window_end)
        except Exception:
            # corrupt / absent table → behave as if no history
            return empty.copy(), empty.copy()

        # ensure every nurse key exists
        for n in self.nurses:
            main.setdefault(n, 0)
            backup.setdefault(n, 0)

        return main, backup

    def _historic_overage(self) -> dict[str, int]:
        """
        For every nurse, compute:
            (30-day total assignments) − (median of all totals)
        Positive values mean that nurse was *over-utilised* last month.
        """
        totals = {
            n: self._historical_main.get(n, 0) + self._historical_backup.get(n, 0)
            for n in self.nurses
        }
        if not totals:
            return {n: 0 for n in self.nurses}

        median_val = round(float(np.median(list(totals.values()))))
        return {n: totals[n] - median_val for n in self.nurses}

    # =====================================================================
    # SCORING AND EVALUATION METHODS
    # =====================================================================

    @staticmethod
    def _long_term_score(nurse_counts: dict[str, dict[str, int]], overage: dict[str, int]) -> int:
        """
        Penalty used when sorting variants.
        For each nurse:
            extra = max(0, overage[n] + variant_total - min_total)
        We sum those extras.  Lower score = variant that helps previously
        over-used nurses land at or below the minimum total in this run.
        """
        if not nurse_counts:
            return 0

        min_total = min(c["total"] for c in nurse_counts.values())

        penalty = 0
        for n, counts in nurse_counts.items():
            penalty += max(0, overage.get(n, 0) + counts["total"] - min_total)
        return penalty

    # --- Add inside NurseScheduler ---------------------------------------------

    def _fridays_in_range(self, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
        """All Fridays in [start, end]."""
        start = DateUtils.normalize_date(start)
        end = DateUtils.normalize_date(end)
        # jump to first Friday on/after start
        offset = (self.FRIDAY_WEEKDAY - start.weekday()) % 7
        cur = start + timedelta(days=offset)
        out: list[pd.Timestamp] = []
        while cur <= end:
            out.append(cur)
            cur += timedelta(days=7)
        return out

    def _as_friday(self, dt: pd.Timestamp | None) -> pd.Timestamp | None:
        """Map any date within a weekend to its Friday (or return None)."""
        if dt is None:
            return None
        return dt - timedelta(days=(dt.weekday() - self.FRIDAY_WEEKDAY) % 7)

    def _rotation_violation_score(
        self,
        nurse_counts: dict[str, dict[str, int]],
        historic_viol: dict[str, int],
        sched_df: pd.DataFrame,
    ) -> int:
        """
        Candidate-sensitive additive penalty.

        Historic repeat counts are weighted by how often a nurse appears in the
        candidate's weekend rows (Fri/Sat/Sun). This differentiates variants
        that use high-violation nurses more heavily on weekends.
        """
        if sched_df is None or sched_df.empty:
            return 0

        weekend_rows = sched_df.loc[sched_df.index.weekday.isin([4, 5, 6]), ["main", "backup"]]
        if weekend_rows.empty:
            return 0

        weekend_appearances: dict[str, int] = defaultdict(int)
        for _, roles in weekend_rows.iterrows():
            for nurse in roles.tolist():
                if is_empty(nurse):
                    continue
                weekend_appearances[str(nurse)] += 1

        # Keep scope to nurses represented in this candidate's counts.
        return sum(
            historic_viol.get(nurse, 0) * weekend_appearances.get(nurse, 0)
            for nurse in nurse_counts
        )

    # ────────────────────────────────────────────────────────────────────
    # NurseScheduler._weekend_gap_penalty
    # ────────────────────────────────────────────────────────────────────
    def _weekend_gap_penalty(self, schedule_df: pd.DataFrame) -> int:
        """Penalty based on *all* consecutive Friday-to-Friday gaps per nurse.

        We build each nurse's timeline by merging:
          • historic weekends before ``start_date`` (from ``weekend_history``),
          • every Friday in ``schedule_df`` where the nurse is assigned, and
          • a forward-looking horizon Friday after ``end_date`` to recognise
            nurses who are not scheduled again within the window.

        Consecutive gaps are measured only when the later Friday falls inside
        or beyond the candidate schedule (``>= start_date``).  The resulting
        penalty prefers variants that maximise those gaps while keeping them
        balanced – reassigning a recently worked nurse later in the period now
        incurs a tangible cost.
        """

        # Collect every Friday assignment inside the candidate schedule
        friday_rows = schedule_df.loc[
            schedule_df.index.weekday == self.FRIDAY_WEEKDAY, ["main", "backup"]
        ]

        weekend_assignments: dict[str, list[pd.Timestamp]] = defaultdict(list)
        for friday, roles in friday_rows.iterrows():
            seen: set[str] = set()
            for nurse in roles.tolist():
                if is_empty(nurse):
                    continue
                if nurse in seen:
                    continue
                seen.add(nurse)
                weekend_assignments.setdefault(nurse, []).append(self._as_friday(friday))

        # Ensure we examine every nurse we actively schedule
        all_nurses: set[str] = set(self.nurses) | set(weekend_assignments.keys())

        # Forward horizon (first Friday on/after end_date + gap window)
        horizon_base = self.end_date + timedelta(days=self.config.weekend_gap_days)
        horizon_offset = (self.FRIDAY_WEEKDAY - horizon_base.weekday()) % 7
        horizon_friday = horizon_base + timedelta(days=horizon_offset)

        target_gap = int(self.config.weekend_gap_days)
        all_gaps: list[int] = []

        for nurse in sorted(all_nurses):
            history_fridays = [
                self._as_friday(DateUtils.normalize_date(hist))
                for hist in self.weekend_history.get_weekends(nurse)
            ]
            history_fridays = [d for d in history_fridays if d is not None and d < self.start_date]
            history_fridays = sorted(set(history_fridays))

            if not history_fridays:
                synthetic_prev = self._as_friday(self.start_date - timedelta(days=target_gap + 1))
                history_fridays = [synthetic_prev]

            scheduled_fridays = sorted(set(weekend_assignments.get(nurse, [])))

            timeline = sorted(set(history_fridays + scheduled_fridays))
            if not timeline:
                timeline = history_fridays[:]

            if not timeline:
                # Should not happen, but keep defensive guard
                continue

            if timeline[-1] < horizon_friday:
                timeline.append(horizon_friday)

            nurse_gaps = []
            for prev, curr in pairwise(timeline):
                if curr < self.start_date:
                    continue
                gap_days = (curr - prev).days
                if gap_days <= 0:
                    continue
                nurse_gaps.append(gap_days)
                all_gaps.append(gap_days)

            if not nurse_gaps:
                # No future assignments → still account for horizon bridge
                gap_days = (timeline[-1] - timeline[-2]).days if len(timeline) >= 2 else 0
                if gap_days > 0:
                    all_gaps.append(gap_days)

        if not all_gaps:
            return 0

        min_gap = min(all_gaps)
        max_gap = max(all_gaps)
        spread = max_gap - min_gap
        deficit = sum(max(0, target_gap - gap) for gap in all_gaps)

        return int(spread + deficit)

    # =====================================================================
    # SCHEDULE INITIALIZATION METHODS
    # =====================================================================

    def _initialize_schedule(self) -> pd.DataFrame:
        """Initialize the main schedule DataFrame with basic structure."""
        dates = pd.date_range(self.start_date, self.end_date, freq="D")
        schedule = pd.DataFrame(index=dates, columns=["main", "backup"])
        schedule["main"] = None
        schedule["backup"] = None
        schedule["day_of_week"] = schedule.index.day_name()
        schedule["is_weekend"] = schedule["day_of_week"].isin(self.WEEKDAYS)
        return schedule

    def _initialize_availability(self) -> pd.DataFrame:
        """Initialize nurse availability matrix."""
        availability = pd.DataFrame(True, index=self.schedule.index, columns=self.nurses)

        for nurse in self.nurses:
            unavailable_dates = self.nurse_manager.get_unavailable_dates(nurse)
            unavailable_dates = pd.to_datetime(list(unavailable_dates))
            valid_dates = set(unavailable_dates).intersection(set(availability.index))
            availability.loc[list(valid_dates), nurse] = False

        return availability

    # =====================================================================
    # PRE-SCHEDULING METHODS
    # =====================================================================

    def _collect_pre_scheduled_slots(self) -> dict[pd.Timestamp, dict[str, str]]:
        """
        Return {date → {"main": name_or_None, "backup": name_or_None}}
        for every pre-scheduled row inside the requested period.
        """
        raw = self.pre_scheduler.get_assignments_in_range(self.start_date, self.end_date)
        slots: dict[pd.Timestamp, dict[str, str]] = {}

        for day_str, row in raw.items():
            ts = DateUtils.normalize_date(day_str)
            slots[ts] = {"main": row.get("main"), "backup": row.get("backup")}

        # Days of an already-recorded weekend that the range cuts are fixed,
        # like pre-scheduled cells; an explicit pre-scheduled name wins.
        for day, roles in self._recorded_edge_weekend_slots().items():
            slot = slots.setdefault(day, {"main": None, "backup": None})
            for role, nurse in roles.items():
                if self.is_empty(slot.get(role)):
                    slot[role] = nurse
        return slots

    def _recorded_weekend(self, friday: pd.Timestamp) -> tuple[str, str] | None:
        """The recorded ``(fsf, sfs)`` pair for a weekend, or None.

        Falls back to "some nurse worked it" (with unknown roles) for history
        objects without ``get_assignment``.
        """
        lookup = getattr(self.weekend_history, "get_assignment", None)
        if lookup is not None:
            pair = lookup(friday)
            if pair and not self.is_empty(pair[0]) and not self.is_empty(pair[1]):
                return pair[0], pair[1]
            return None
        for nurse in self.nurses:
            if friday in self.weekend_history.get_weekends(nurse):
                return (None, None)
        return None

    def _recorded_edge_weekend_slots(self) -> dict[pd.Timestamp, dict[str, str]]:
        """Cells of recorded weekends that straddle the window's start or end.

        :func:`whole_weekend_range` leaves such a weekend out of the widened
        range because it belongs to the period that recorded it; the days of
        it that do fall inside this window keep their recorded roles.
        """
        slots: dict[pd.Timestamp, dict[str, str]] = {}
        edge_fridays = {self._as_friday(self.start_date), self._as_friday(self.end_date)}
        for friday in edge_fridays:
            if self.start_date <= friday and friday + timedelta(days=2) <= self.end_date:
                continue  # a whole weekend inside the window: generated normally
            pair = self._recorded_weekend(friday)
            if not pair or self.is_empty(pair[0]):
                continue
            fsf, sfs = pair
            pattern = ((fsf, sfs), (sfs, fsf), (fsf, sfs))  # Fri, Sat, Sun (main, backup)
            for offset, (main, backup) in enumerate(pattern):
                day = friday + timedelta(days=offset)
                if self.start_date <= day <= self.end_date:
                    slots[day] = {"main": main, "backup": backup}
        return slots

    # --- Replace in NurseScheduler ---------------------------------------------

    def _get_pre_scheduled_weekend_assignments(self) -> dict:
        """
        Extract pre-scheduled weekend assignments for:
          • the schedule window, and
          • an additional forward horizon = weekend_gap_days (+2 for Sat/Sun)
        so forward-gap checks can block end-of-window conflicts.
        """
        # extend scan to capture the first full weekend after gap_min
        horizon_end = (self.end_date + timedelta(days=self.config.weekend_gap_days + 2)).normalize()

        # pull pre-scheduled rows across the extended range
        pre_scheduled = self.pre_scheduler.get_assignments_in_range(self.start_date, horizon_end)
        pre_scheduled_df = pd.DataFrame.from_dict(pre_scheduled, orient="index")
        if not pre_scheduled_df.empty:
            pre_scheduled_df.index = pd.DatetimeIndex(
                [DateUtils.normalize_date(idx) for idx in pre_scheduled_df.index]
            )

        # build Friday keys for both the in-window weekends and the forward horizon
        fridays = self._fridays_in_range(self.start_date, horizon_end)
        weekend_assignments = {
            friday: {WeekendPattern.FSF: None, WeekendPattern.SFS: None} for friday in fridays
        }

        # direct Friday hints (Fri main → FSF, Fri backup → SFS)
        if not pre_scheduled_df.empty:
            friday_rows = pre_scheduled_df.loc[
                pre_scheduled_df.index.weekday == self.FRIDAY_WEEKDAY
            ]
            for friday in fridays:
                if friday in friday_rows.index:
                    fri_main = (
                        friday_rows.at[friday, "main"] if "main" in friday_rows.columns else None
                    )
                    fri_backup = (
                        friday_rows.at[friday, "backup"]
                        if "backup" in friday_rows.columns
                        else None
                    )
                    if fri_main and not self.is_empty(fri_main):
                        weekend_assignments[friday][WeekendPattern.FSF] = fri_main
                    if fri_backup and not self.is_empty(fri_backup):
                        weekend_assignments[friday][WeekendPattern.SFS] = fri_backup

            # infer FSF/SFS when whole-weekend is prefilled but Friday wasn't explicit
            self._infer_weekend_patterns_from_whole_weekend(
                weekend_assignments, pre_scheduled_df, fridays
            )

        return weekend_assignments

    def _infer_weekend_patterns_from_whole_weekend(
        self, weekend_assignments, pre_scheduled_df, fridays
    ):
        """Infer FSF/SFS patterns from whole weekend assignments when not explicitly set."""
        for friday in fridays:
            if self.is_empty(weekend_assignments[friday][WeekendPattern.FSF]) and self.is_empty(
                weekend_assignments[friday][WeekendPattern.SFS]
            ):
                weekend_dates = [
                    friday,
                    friday + pd.Timedelta(days=1),
                    friday + pd.Timedelta(days=2),
                ]
                weekend_df = pre_scheduled_df.loc[pre_scheduled_df.index.isin(weekend_dates)]
                nurse_counts = {}

                for _, row in weekend_df.iterrows():
                    self._count_nurse_assignments(row, nurse_counts)

                # Only assign if a nurse is not assigned to both roles
                for nurse, counts in nurse_counts.items():
                    if (
                        counts["main"] + counts["backup"] >= 2
                        and counts["main"] != counts["backup"]
                    ):
                        pattern = (
                            WeekendPattern.FSF
                            if counts["main"] > counts["backup"]
                            else WeekendPattern.SFS
                        )
                        weekend_assignments[friday][pattern] = nurse

    def _count_nurse_assignments(self, row, nurse_counts):
        """Count main and backup assignments for nurses in a given row."""
        main_nurse = row.get("main")
        backup_nurse = row.get("backup")

        if main_nurse and not self.is_empty(main_nurse):
            nurse_counts.setdefault(main_nurse, {"main": 0, "backup": 0})["main"] += 1
        if backup_nurse and not self.is_empty(backup_nurse):
            nurse_counts.setdefault(backup_nurse, {"main": 0, "backup": 0})["backup"] += 1

    # =====================================================================
    # WEEKEND MANAGEMENT METHODS
    # =====================================================================

    def _get_weekends(self) -> list[pd.Timestamp]:
        """
        Return every Friday for which the *whole* Fri-Sat-Sun block lies
        inside [start_date … end_date] (i.e. friday + 2 ≤ end_date).
        """
        weekends: list[pd.Timestamp] = []

        # latest Friday that can still form a complete weekend
        last_allowed_fri = (self.end_date - timedelta(days=2)).normalize()

        current = self.start_date.normalize()
        while current <= last_allowed_fri:
            if current.weekday() == self.FRIDAY_WEEKDAY:  # Friday
                weekends.append(current)
            current += timedelta(days=1)

        return weekends

    def _weekend_dates(self, friday: pd.Timestamp) -> list[pd.Timestamp]:
        """Return the three dates of a weekend given its Friday."""
        return [friday, friday + timedelta(days=1), friday + timedelta(days=2)]

    # --- Replace in NurseScheduler ---------------------------------------------

    def _get_next_weekend_assignment(
        self, nurse, current_weekend, schedule, all_pre_scheduled_weekends=None
    ):
        """
        Earliest *future* weekend (as its Friday) on which nurse is assigned,
        considering the current schedule, all pre-scheduled weekends (which
        may extend beyond the schedule window), and weekends already recorded
        in weekend history after the window.
        """
        # From the current schedule (within window)
        weekend_mask = schedule["day_of_week"].isin(self.WEEKDAYS)
        weekend_df = schedule.loc[weekend_mask]
        cut_off = current_weekend + timedelta(days=2)  # after this Sunday
        future_weekends = weekend_df.loc[weekend_df.index > cut_off]
        assigned = future_weekends[
            (future_weekends["main"] == nurse) | (future_weekends["backup"] == nurse)
        ]
        next_in_schedule = assigned.index.min() if not assigned.empty else None
        next_in_schedule = self._as_friday(next_in_schedule)

        # From pre-scheduled weekends (can be beyond window)
        next_in_pre = self._find_next_pre_scheduled_weekend(
            nurse, current_weekend, all_pre_scheduled_weekends
        )
        next_in_pre = self._as_friday(next_in_pre)

        # Weekends already recorded after the window
        next_recorded = next(
            (f for f in self._future_weekends().get(nurse, ()) if f > current_weekend), None
        )

        candidates = [d for d in (next_in_schedule, next_in_pre, next_recorded) if d is not None]
        return min(candidates) if candidates else None

    def _find_next_pre_scheduled_weekend(self, nurse, current_weekend, all_pre_scheduled_weekends):
        """Find the next pre-scheduled weekend for a nurse."""
        next_in_pre = None
        if all_pre_scheduled_weekends:
            for wk, roles in all_pre_scheduled_weekends.items():
                if wk > current_weekend and nurse in roles.values():
                    if next_in_pre is None or wk < next_in_pre:
                        next_in_pre = wk
        return next_in_pre

    # =====================================================================
    # NURSE VALIDATION METHODS
    # =====================================================================

    def _is_nurse_available_for_weekend(self, nurse: str, weekend: pd.Timestamp) -> bool:
        """Check if a nurse is available for all three days of a weekend."""
        if self.nurse_manager.is_prn_nurse(nurse):
            return False

        weekend_dates = self._weekend_dates(weekend)
        weekend_dates_in_idx = [d for d in weekend_dates if d in self.availability.index]
        if len(weekend_dates_in_idx) != self.WEEKEND_DAYS_COUNT:
            return False

        try:
            vals = self.availability.loc[weekend_dates_in_idx, nurse]
        except KeyError:
            return False

        return bool(vals.apply(lambda v: (not pd.isna(v)) and bool(v)).all())

    def _check_weekend_gap_constraints(
        self,
        nurse: str,
        weekend: pd.Timestamp,
        last_assignment: dict,  # kept for signature compatibility; not used
        schedule: pd.DataFrame,
        all_pre_scheduled_weekends: dict,
    ) -> bool:
        """
        Hard rule: a nurse must have strictly more than config.weekend_gap_days
        between Fridays of two worked weekends — both backward and forward.

        This uses weekend history and the current schedule (including any
        pre-scheduled weekends) to decide.
        """
        gap_min = self.config.weekend_gap_days

        # ── backward gap: use historic last and any prior worked weekend in this schedule ──
        # History counts only before the window: weekends recorded inside it
        # belong to the schedule being replaced, and this branch's own earlier
        # weekends are found in `schedule` below. Normalize the history date to
        # its Friday so the day-diff compares Friday→Friday, matching
        # _weekend_gap_penalty's treatment of history.
        prev_wk_hist = self._as_friday(
            self.weekend_history.get_last_weekend_before(nurse, min(weekend, self.start_date))
        )

        prev_wk_sched = None
        prior_fridays = [
            d for d in schedule.index if d.weekday() == self.FRIDAY_WEEKDAY and d < weekend
        ]
        for f in reversed(prior_fridays):
            # Check Fri/Sat/Sun block for this nurse
            block = [f, f + pd.Timedelta(days=1), f + pd.Timedelta(days=2)]
            found = False
            for day in block:
                if day in schedule.index:
                    if (schedule.at[day, "main"] == nurse) or (schedule.at[day, "backup"] == nurse):
                        prev_wk_sched = f
                        found = True
                        break
            if found:
                break

        # Choose the latest of the two candidates
        if prev_wk_hist is not None and prev_wk_sched is not None:
            prev_wk = max(prev_wk_hist, prev_wk_sched)
        else:
            prev_wk = prev_wk_hist or prev_wk_sched

        if prev_wk is not None:
            # Compare Friday→Friday
            if (weekend - prev_wk).days <= gap_min:
                return False

        # ── forward gap: first future worked weekend from schedule or pre-scheduled ──
        next_wk = self._get_next_weekend_assignment(
            nurse, weekend, schedule, all_pre_scheduled_weekends
        )
        if next_wk is not None:
            # Ensure we compare Friday→Friday regardless of which day was assigned
            next_friday = self._as_friday(next_wk)
            if (next_friday - weekend).days <= gap_min:
                return False

        return True

    def _check_rotation_constraints(
        self,
        nurse: str,
        last_pattern: dict,
        enforce_rotation: bool,
        nurses_allowed_rotation_violation: set[str],
    ) -> tuple[bool, bool]:
        """
        Check rotation constraints for a nurse.
        Returns (can_do_fsf, can_do_sfs)
        """
        last_pat = last_pattern.get(nurse)
        if last_pat is None:
            return True, True  # No prior pattern → both patterns possible

        if enforce_rotation:
            # Strict: only the opposite pattern is allowed
            if last_pat == WeekendPattern.FSF:
                return False, True  # eligible SFS only
            if last_pat == WeekendPattern.SFS:
                return True, False  # eligible FSF only
        else:
            # Relaxed: may repeat only if nurse is whitelisted
            if nurse in nurses_allowed_rotation_violation:
                return True, True  # eligible both (violation allowed)
            else:
                if last_pat == WeekendPattern.FSF:
                    return False, True  # eligible SFS only
                if last_pat == WeekendPattern.SFS:
                    return True, False  # eligible FSF only

        return True, True

    # =====================================================================
    # NURSE PAIR VALIDATION METHODS
    # =====================================================================

    def _get_valid_nurse_pairs(
        self,
        weekend: pd.Timestamp,
        last_assignment: dict,
        last_pattern: dict,
        pre_scheduled: dict,
        weekend_tracking: dict,
        *,
        schedule: pd.DataFrame | None = None,
        all_pre_scheduled_weekends: dict | None = None,
        enforce_rotation: bool = True,
        nurses_allowed_rotation_violation: set[str] | None = None,
        **_ignored_kwargs,
    ) -> list[tuple[str, str]]:
        """
        Return every (fsf_nurse, sfs_nurse) pair that satisfies every
        hard-rule (availability, gap, rotation, etc.).
        """
        # Normalize optional arguments
        schedule = schedule if schedule is not None else self.schedule
        nurses_allowed_rotation_violation = (
            set()
            if nurses_allowed_rotation_violation is None
            else set(nurses_allowed_rotation_violation)
        )

        # If we are in the relaxed branch and caller supplied empty set,
        # interpret as "every nurse may violate"
        if (not enforce_rotation) and (not nurses_allowed_rotation_violation):
            nurses_allowed_rotation_violation = set(self.nurses)

        # Get pre-scheduled names
        fsf_pre = pre_scheduled.get(WeekendPattern.FSF)
        sfs_pre = pre_scheduled.get(WeekendPattern.SFS)

        # Get valid nurses for each pattern
        valid_fsf, valid_sfs = self._get_valid_nurses_for_patterns(
            weekend,
            last_assignment,
            last_pattern,
            schedule,
            all_pre_scheduled_weekends,
            enforce_rotation,
            nurses_allowed_rotation_violation,
        )

        # Force-include any fixed names that the filters might have pruned
        self._ensure_pre_scheduled_nurses_included(fsf_pre, sfs_pre, valid_fsf, valid_sfs)

        # Build pairs respecting fixed assignments
        pairs = self._build_nurse_pairs(fsf_pre, sfs_pre, valid_fsf, valid_sfs)

        # Reject pairs that conflict with any non-empty prefilled weekend cells.
        pairs = [
            (fsf, sfs)
            for fsf, sfs in pairs
            if self._pair_matches_prefilled_weekend_cells(schedule, weekend, fsf, sfs)
        ]

        # Filter out invalid late-shift combinations
        return self._filter_late_shift_pairs(pairs, fsf_pre, sfs_pre)

    def _pair_matches_prefilled_weekend_cells(
        self,
        schedule: pd.DataFrame,
        weekend: pd.Timestamp,
        fsf_nurse: str,
        sfs_nurse: str,
    ) -> bool:
        """
        Return True iff (fsf_nurse, sfs_nurse) is compatible with any existing
        non-empty Friday/Saturday/Sunday main/backup cells.
        """
        implied_assignments = (
            (weekend, "main", fsf_nurse),  # Fri main  = FSF
            (weekend, "backup", sfs_nurse),  # Fri backup= SFS
            (weekend + timedelta(days=1), "main", sfs_nurse),  # Sat main  = SFS
            (weekend + timedelta(days=1), "backup", fsf_nurse),  # Sat backup= FSF
            (weekend + timedelta(days=2), "main", fsf_nurse),  # Sun main  = FSF
            (weekend + timedelta(days=2), "backup", sfs_nurse),  # Sun backup= SFS
        )

        for day, role, expected_nurse in implied_assignments:
            if day not in schedule.index:
                continue
            prefilled_nurse = schedule.at[day, role]
            if self.is_empty(prefilled_nurse):
                continue
            if prefilled_nurse != expected_nurse:
                return False

        return True

    def _get_valid_nurses_for_patterns(
        self,
        weekend,
        last_assignment,
        last_pattern,
        schedule,
        all_pre_scheduled_weekends,
        enforce_rotation,
        nurses_allowed_rotation_violation,
    ):
        """Get lists of valid nurses for FSF and SFS patterns."""
        valid_fsf: list[str] = []
        valid_sfs: list[str] = []

        weekend_dates_in_idx = [
            d for d in self._weekend_dates(weekend) if d in self.availability.index
        ]

        _dbg_pairs("\n--- _get_valid_nurse_pairs ---")
        _dbg_pairs(f"Weekend: {weekend.date()}")

        for nurse in self.nurses:
            # Check basic eligibility
            if not self._is_nurse_eligible_for_weekend(
                nurse,
                weekend_dates_in_idx,
                last_assignment,
                weekend,
                schedule,
                all_pre_scheduled_weekends,
            ):
                continue

            # Check rotation constraints
            can_fsf, can_sfs = self._check_rotation_constraints(
                nurse, last_pattern, enforce_rotation, nurses_allowed_rotation_violation
            )

            if can_fsf:
                valid_fsf.append(nurse)
            if can_sfs:
                valid_sfs.append(nurse)

        return valid_fsf, valid_sfs

    def _is_nurse_eligible_for_weekend(
        self,
        nurse,
        weekend_dates_in_idx,
        last_assignment,
        weekend,
        schedule,
        all_pre_scheduled_weekends,
    ):
        """Check basic eligibility for weekend assignment (PRN, availability, gap)."""
        # PRN staff never work weekends
        if self.nurse_manager.is_prn_nurse(nurse):
            _reject(nurse, "PRN")
            return False

        # Must be available for all three days (NaN = unavailable)
        if len(weekend_dates_in_idx) != self.WEEKEND_DAYS_COUNT:
            _reject(nurse, "Unavailable for full weekend")
            return False

        try:
            vals = self.availability.loc[weekend_dates_in_idx, nurse]
        except KeyError:
            _reject(nurse, "Unavailable for full weekend")
            return False

        if not vals.apply(lambda v: (not pd.isna(v)) and bool(v)).all():
            _reject(nurse, "Unavailable for full weekend")
            return False

        # Weekend gap constraints (backward via history/schedule, forward via schedule/pre-scheduled)
        if not self._check_weekend_gap_constraints(
            nurse, weekend, last_assignment, schedule, all_pre_scheduled_weekends
        ):
            return False

        return True

    def _ensure_pre_scheduled_nurses_included(self, fsf_pre, sfs_pre, valid_fsf, valid_sfs):
        """Ensure pre-scheduled nurses are included in valid lists."""
        if fsf_pre and fsf_pre not in valid_fsf:
            valid_fsf.append(fsf_pre)
        if sfs_pre and sfs_pre not in valid_sfs:
            valid_sfs.append(sfs_pre)

    def _build_nurse_pairs(self, fsf_pre, sfs_pre, valid_fsf, valid_sfs):
        """Build nurse pairs respecting any fixed assignments."""
        if fsf_pre and sfs_pre:
            pairs = [(fsf_pre, sfs_pre)] if fsf_pre != sfs_pre else []
        elif fsf_pre:
            pairs = [(fsf_pre, s) for s in valid_sfs if s != fsf_pre]
        elif sfs_pre:
            pairs = [(f, sfs_pre) for f in valid_fsf if f != sfs_pre]
        else:
            pairs = [(f, s) for f in valid_fsf for s in valid_sfs if f != s]
        return pairs

    def _filter_late_shift_pairs(
        self, pairs, fsf_pre: str | None = None, sfs_pre: str | None = None
    ):
        """Filter out late/late pairs unless the combination is fully pre-scheduled."""
        result: list[tuple[str, str]] = []
        allow_prescheduled_pair = bool(fsf_pre and sfs_pre)
        for f, s in pairs:
            both_late = self.nurse_manager.is_late_shift_nurse(
                f
            ) and self.nurse_manager.is_late_shift_nurse(s)
            if both_late:
                if allow_prescheduled_pair and f == fsf_pre and s == sfs_pre:
                    _pair(f, s)
                    result.append((f, s))
                continue
            _pair(f, s)
            result.append((f, s))
        return result

    # =====================================================================
    # VARIANT GENERATION METHODS
    # =====================================================================

    def generate_all_weekend_variants(
        self, *, allow_rotation_violations: bool = False
    ) -> list[ScheduleVariant]:
        """
        Build every feasible schedule variant. Logs branching and state to debug_variants.txt.
        """
        if _runtime._DBG_FILE_VARIANTS:
            _runtime._DBG_FILE_VARIANTS.seek(0)
            _runtime._DBG_FILE_VARIANTS.truncate()
        _dbg_variants("=== generate_all_weekend_variants debug ===")

        self.rotation_violation_history = defaultdict(list)
        self._rotation_violations = []
        self._rotation_enforced = True

        try:
            weekends = self._get_weekends()
            pre_weekend_assignments = self._get_pre_scheduled_weekend_assignments()
            pre_scheduled_slots = self._collect_pre_scheduled_slots()

            initial = ScheduleVariant(
                self.get_state_snapshot(),
                self.nurses,
                self.availability,
                self.config,
                self.nurse_manager,
                pre_scheduled_slots,
            )
            variants = [initial]
            max_variants = int(getattr(self.config, "max_weekend_variants", 0) or 0)
            pruned_any = False

            for friday in weekends:
                variants = self._process_weekend_variants(
                    friday, variants, pre_weekend_assignments, allow_rotation_violations
                )
                if not variants:
                    _dbg_variants(f"  ERROR: no variants left after {friday.date()}")
                    if pruned_any:
                        logger.warning(
                            "No feasible variants left after %s, but earlier weekends "
                            "were pruned to max_weekend_variants=%d. Raising that limit "
                            "may recover a feasible schedule.",
                            friday.date(),
                            max_variants,
                        )
                    return []
                if max_variants and len(variants) > max_variants:
                    variants = self._prune_weekend_variants(variants, max_variants, friday)
                    pruned_any = True

            self._collect_rotation_violations(variants)
            _dbg_variants(f"\nFinal total variants: {len(variants)}")
            return variants

        except Exception:
            # Log unconditionally so a genuine crash is not silently reported as
            # "no feasible schedule" (the debug sink is off unless NSCHED_DEBUG).
            logger.error("generate_all_weekend_variants failed:\n%s", traceback.format_exc())
            _dbg_variants("EXCEPTION:\n")
            _dbg_variants(traceback.format_exc())
            return []

    def _prune_weekend_variants(self, variants, max_variants, friday):
        """
        Trim the variant beam to ``max_variants`` after one weekend's branching.

        Growth is roughly (valid pairs)^(weekends) and each survivor later runs
        the full clone → assign → gap-fill → rebalance pipeline, so an
        unbounded beam can stall a whole generation run. Variants are kept by
        a cheap weekend-only preference: fewest rotation repeats introduced on
        the branch, then most even spread of weekends across nurses (history
        included), then the largest minimum Friday-to-Friday gap. Sorting is
        stable, so ties keep their original deterministic order.
        """

        def prune_key(variant):
            lists = variant.state.nurse_weekend_lists
            counts = [len(lists.get(n, ())) for n in self.nurses]
            imbalance = (max(counts) - min(counts)) if counts else 0
            min_gap = None
            for fridays in lists.values():
                for prev, nxt in pairwise(fridays):
                    gap = (nxt - prev).days
                    if min_gap is None or gap < min_gap:
                        min_gap = gap
            return (
                len(variant.rotation_violations),
                imbalance,
                -(min_gap if min_gap is not None else 10**6),
            )

        logger.warning(
            "Weekend %s produced %d variants; pruning beam to best %d (max_weekend_variants).",
            friday.date(),
            len(variants),
            max_variants,
        )
        _dbg_variants(f"  pruning {len(variants)} variants to {max_variants} after {friday.date()}")
        return sorted(variants, key=prune_key)[:max_variants]

    def _process_weekend_variants(
        self, friday, variants, pre_weekend_assignments, allow_rotation_violations
    ):
        """Process variants for a specific weekend."""
        _dbg_variants(f"\n--- Weekend {friday.date()} ---")
        _dbg_variants(f"  starting variants count: {len(variants)}")

        for i, var in enumerate(variants):
            _dbg_variants(
                f"    VAR#{i} last_assign: {var.state.last_assignment}  "
                f"last_pat: {var.state.last_pattern}"
            )

        next_vars: list[ScheduleVariant] = []
        fixed = pre_weekend_assignments.get(friday, {})

        if not allow_rotation_violations:
            # Strict pass only. No silent per-weekend fallback to the relaxed
            # branch: callers that want repeats must pass
            # allow_rotation_violations=True (the STRICT_THEN_RELAXED flow does
            # this after the confirm_rotation_callback gate approves it).
            next_vars = self._generate_strict_variants(
                variants, friday, fixed, pre_weekend_assignments
            )
            _dbg_variants(f"  after strict pass: {len(next_vars)} variants")
        else:
            next_vars = self._generate_relaxed_variants(
                variants, friday, fixed, pre_weekend_assignments, next_vars
            )
            _dbg_variants(f"  after repeat-allowed pass: {len(next_vars)} variants")

        return next_vars

    def _generate_strict_variants(self, variants, friday, fixed, pre_weekend_assignments):
        """Generate variants with strict rotation enforcement."""
        next_vars = []
        for var in variants:
            pairs = self._get_valid_nurse_pairs(
                friday,
                var.state.last_assignment,
                var.state.last_pattern,
                fixed,
                var.state.weekend_tracking,
                schedule=var.state.schedule,
                all_pre_scheduled_weekends=pre_weekend_assignments,
                enforce_rotation=True,
            )
            for fsf, sfs in pairs:
                clone = var.clone()
                clone.assign_weekend(friday, fsf, sfs)
                next_vars.append(clone)
        return next_vars

    def _generate_relaxed_variants(
        self, variants, friday, fixed, pre_weekend_assignments, next_vars
    ):
        """Generate variants with relaxed rotation rules."""
        self._rotation_enforced = False
        for var in variants:
            pairs = self._get_valid_nurse_pairs(
                friday,
                var.state.last_assignment,
                var.state.last_pattern,
                fixed,
                var.state.weekend_tracking,
                schedule=var.state.schedule,
                all_pre_scheduled_weekends=pre_weekend_assignments,
                enforce_rotation=False,
                nurses_allowed_rotation_violation=self.nurses_allowed_rotation_violation,
            )
            for fsf, sfs in pairs:
                clone = var.clone()
                # assign_weekend records any rotation repeat on the clone
                # itself, so violations stay attributable to the branch that
                # actually contains them.
                clone.assign_weekend(friday, fsf, sfs)
                next_vars.append(clone)
        return next_vars

    def _collect_rotation_violations(self, variants) -> None:
        """
        Rebuild scheduler-level rotation-violation reporting from the
        surviving variants' per-branch records.

        Each distinct (weekend, nurse, pattern) is counted once, no matter how
        many surviving branches share it — unlike the old per-(parent × pair)
        tracking, pruned branches contribute nothing and shared ancestry does
        not inflate the counts.
        """
        self.rotation_violation_history = defaultdict(list)
        self._rotation_violations = []
        seen: set[tuple] = set()
        for var in variants:
            for friday, nurse, pattern_value in getattr(var, "rotation_violations", []):
                key = (friday, nurse, pattern_value)
                if key in seen:
                    continue
                seen.add(key)
                self.rotation_violation_history[nurse].append(friday)
                self._rotation_violations.append((friday.isoformat(), nurse, pattern_value))

    # =====================================================================
    # STATE AND UTILITY METHODS
    # =====================================================================

    def get_state_snapshot(self) -> ScheduleState:
        """Get a snapshot of the current scheduling state."""
        future = self._future_weekends()
        weekend_lists = {}
        for nurse in self.nurses:
            historic = [w for w in self.weekend_history.get_weekends(nurse) if w < self.start_date]
            # Weekends already committed after the window bound the
            # pre-weekend window of the window's last days, just as history
            # bounds the post-weekend window of its first days.
            weekend_lists[nurse] = sorted(set(historic) | set(future.get(nurse, ())))

        return ScheduleState(
            self.schedule,
            self.main_assignment_counts,
            self.backup_assignment_counts,
            self.last_assignment,
            self.last_pattern,
            self.weekend_tracking,
            nurse_weekend_lists=weekend_lists,
            pre_window_worked=self._collect_pre_window_worked_days(),
            post_window_worked=self._collect_post_window_worked_days(),
        )

    def _collect_pre_window_worked_days(self) -> dict[str, set[pd.Timestamp]]:
        """
        Collect the days each nurse worked in the ``min_days_between_assignments``
        days immediately before ``start_date``.

        The in-window spacing check can only see schedule cells inside the
        window, so without this a shift worked the day before ``start_date``
        (a weekday shift from the persisted per-day history, or the tail of a
        weekend) is invisible to ``min_days_between_assignments``.
        """
        lookback = int(getattr(self.config, "min_days_between_assignments", 0) or 0)
        if lookback <= 0:
            return {}
        return self._collect_worked_days(
            self.start_date - timedelta(days=lookback),
            self.start_date - timedelta(days=1),
        )

    def _collect_post_window_worked_days(self) -> dict[str, set[pd.Timestamp]]:
        """
        Collect the days each nurse is already committed to in the
        ``min_days_between_assignments`` days immediately after ``end_date``:
        recorded weekends, pre-scheduled cells and applied per-day history.
        The mirror of :meth:`_collect_pre_window_worked_days` for the
        window's end.
        """
        lookahead = int(getattr(self.config, "min_days_between_assignments", 0) or 0)
        if lookahead <= 0:
            return {}
        return self._collect_worked_days(
            self.end_date + timedelta(days=1),
            self.end_date + timedelta(days=lookahead),
        )

    def _collect_worked_days(
        self, first: pd.Timestamp, last: pd.Timestamp
    ) -> dict[str, set[pd.Timestamp]]:
        """Days in ``[first, last]`` (outside the window) each nurse works."""
        worked: dict[str, set[pd.Timestamp]] = {n: set() for n in self.nurses}

        def add(nurse, day) -> None:
            if nurse in worked:
                worked[nurse].add(DateUtils.normalize_date(day))

        # Weekend history: FSF/SFS nurses both work Fri, Sat and Sun.
        for nurse in self.nurses:
            for friday in self.weekend_history.get_weekends(nurse):
                for offset in range(3):
                    day = friday + timedelta(days=offset)
                    if first <= day <= last:
                        add(nurse, day)

        # Pre-scheduled cells are fixed commitments on either side.
        for day, row in self.pre_scheduler.get_assignments_in_range(first, last).items():
            for nurse in (row.get("main"), row.get("backup")):
                add(nurse, day)

        # Per-day schedule history covers weekday shifts as well.
        if getattr(self, "assignment_history", None):
            try:
                records = self.assignment_history.get_history(first, last)
            except Exception:
                records = []
            for date_str, main, backup in records:
                for nurse in (main, backup):
                    add(nurse, date_str)

        return {n: days for n, days in worked.items() if days}

    def _future_weekends(self) -> dict[str, list[pd.Timestamp]]:
        """
        Fridays each nurse is already committed to after ``end_date``.

        These come from weekend history (a later period applied first) and
        from pre-scheduled weekends past the window. The forward weekend-gap
        check and the pre-weekend window of the window's last days must
        respect them, or the window's end could put a nurse on the weekend
        before one they already work.
        """
        cached = getattr(self, "_future_weekends_cache", None)
        if cached is not None:
            return cached

        future: dict[str, set[pd.Timestamp]] = {n: set() for n in self.nurses}
        for nurse in self.nurses:
            for weekend in self.weekend_history.get_weekends(nurse):
                friday = self._as_friday(DateUtils.normalize_date(weekend))
                if friday > self.end_date:
                    future[nurse].add(friday)
        for friday, roles in self._get_pre_scheduled_weekend_assignments().items():
            if friday <= self.end_date:
                continue
            for nurse in roles.values():
                if nurse in future:
                    future[nurse].add(friday)

        self._future_weekends_cache = {n: sorted(f) for n, f in future.items() if f}
        return self._future_weekends_cache

    @staticmethod
    def is_empty(value) -> bool:
        """
        Check if a given value is considered empty or unassigned.
        Args:
            value: The value to check (typically a nurse name or assignment).
        Returns:
            True if the value is None, NaN, empty string, or whitespace-only string; False otherwise.
        """
        return is_empty(value)

    def get_nurse_assignment_counts(self, variant: ScheduleVariant) -> dict:
        """Get assignment counts for all nurses in a variant."""
        counts = {}
        for nurse in self.nurses:
            main_count = (variant.state.schedule["main"] == nurse).sum()
            backup_count = (variant.state.schedule["backup"] == nurse).sum()
            counts[nurse] = {
                "main": int(main_count),
                "backup": int(backup_count),
                "total": int(main_count + backup_count),
            }
        return counts

    # =====================================================================
    # CONFIGURATION METHODS
    # =====================================================================

    def set_allow_rotation_violations(self, allow: bool):
        """Set whether rotation violations are allowed."""
        self.allow_rotation_violations = allow

    def set_nurses_allowed_rotation_violation(self, nurses: list[str]):
        """
        Set the set of nurses allowed to violate the rotation rule.
        If nurses is empty and allow_rotation_violations is True, treat as all nurses allowed.
        """
        if getattr(self, "allow_rotation_violations", False) and not nurses:
            self.nurses_allowed_rotation_violation = set(self.nurses)
        else:
            self.nurses_allowed_rotation_violation = set(nurses)

    def get_rotation_violation_history(self) -> dict[str, list[pd.Timestamp]]:
        """Get the history of rotation violations."""
        return self.rotation_violation_history

    # =====================================================================
    # EXPORT AND OUTPUT METHODS
    # =====================================================================

    def export_weekend_variants(self, filename: str):
        """
        Generate the weekend variants (weekend assignments only, before weekday assignment)
        and write them to a text file for manual review.
        """
        weekend_variants = self.generate_all_weekend_variants()
        with open(filename, "w") as f:
            for idx, variant in enumerate(weekend_variants):
                # Extract only the weekend rows from the variant schedule:
                weekend_df = variant.state.schedule[variant.state.schedule["is_weekend"]]
                f.write(f"Variant {idx}\n")
                f.write(weekend_df[["main", "backup"]].to_string())
                f.write("\n" + "-" * 40 + "\n")

    def _export_variant_pdf(
        self, pdf_path: str, sched_df: pd.DataFrame, cal: calendar.Calendar
    ) -> None:
        """Delegate to :func:`scheduler.exporters.pdf.export_variant_pdf`."""
        _export_variant_pdf_fn(pdf_path, sched_df, cal, self.PDF_FONT_SIZES)

    def _draw_weekday_header(self, cvs, hdr_y_top, row_h, col_w):
        _draw_weekday_header_fn(cvs, hdr_y_top, row_h, col_w, self.PDF_FONT_SIZES)

    def _draw_week_rows(self, cvs, weeks, year, month, sched_df, y_top, row_h, col_w):
        _draw_week_rows_fn(
            cvs,
            weeks,
            year,
            month,
            sched_df,
            y_top,
            row_h,
            col_w,
            self.PDF_FONT_SIZES,
        )

    # =====================================================================
    # MAIN SCHEDULE GENERATION METHOD
    # =====================================================================

    class WeekendVariantMode(str, Enum):
        """Controls how weekend variants are generated."""

        STRICT_ONLY = "strict_only"
        STRICT_THEN_RELAXED = "strict_then_relaxed"
        RELAXED_ALLOWED = "relaxed_allowed"

    @classmethod
    def _normalize_weekend_variant_mode(
        cls,
        weekend_variant_mode: str | NurseScheduler.WeekendVariantMode,
    ) -> NurseScheduler.WeekendVariantMode:
        """Normalize weekend variant mode from enum or string input."""
        if isinstance(weekend_variant_mode, cls.WeekendVariantMode):
            return weekend_variant_mode
        try:
            return cls.WeekendVariantMode(weekend_variant_mode)
        except ValueError as ex:
            valid_modes = ", ".join(mode.value for mode in cls.WeekendVariantMode)
            raise ValueError(
                f"Invalid weekend_variant_mode={weekend_variant_mode!r}. "
                f"Expected one of: {valid_modes}."
            ) from ex

    def generate_schedule(
        self,
        top_n: int = 10,
        max_workers: int | None = None,
        *,
        confirm_rotation_callback: Callable[[], bool] | None = None,
        weekend_variant_mode: str
        | NurseScheduler.WeekendVariantMode = WeekendVariantMode.STRICT_THEN_RELAXED,
        profile_performance: bool | None = None,
        profile_output_path: str | os.PathLike[str] | None = None,
    ) -> list:
        """Generate schedules and optionally capture detailed performance metrics.

        ``max_workers`` caps the variant-evaluation process pool; ``None`` sizes
        it to the machine (see :func:`scheduler.platform.default_worker_count`).
        """
        sleep_handle = inhibit_sleep()

        try:
            profiling_enabled = (
                PERFORMANCE_PROFILING_REQUESTED
                if profile_performance is None
                else bool(profile_performance)
            )
            if profiling_enabled and psutil is None:
                logger.warning(
                    "Performance profiling requested but psutil is not available. "
                    "Install psutil or disable profiling to silence this message."
                )
                profiling_enabled = False

            profile_json_path: str | os.PathLike[str] | None
            if profile_output_path is None:
                profile_json_path = PERFORMANCE_PROFILE_JSON_DEFAULT
            else:
                profile_json_path = profile_output_path

            # Setup rotation confirmation callback
            confirm_rotation_callback = self._setup_rotation_callback(confirm_rotation_callback)

            # Generate weekend variants
            variants = self._generate_weekend_variants(
                confirm_rotation_callback, weekend_variant_mode
            )
            if not variants:
                return []

            # Evaluate variants with or without profiling
            if profiling_enabled:
                candidate_schedules, worker_metrics = self._evaluate_variants_with_profiling(
                    variants, max_workers
                )
            else:
                candidate_schedules = self._evaluate_variants(variants, max_workers)
                worker_metrics = []

            if not candidate_schedules:
                logger.error("❌ No candidate schedules after evaluation.")
                return []

            # Score and rank variants
            self._score_and_rank_variants(candidate_schedules)

            # Export best variants as PDFs
            self._export_top_variants_as_pdfs(candidate_schedules, top_n)

            # Optional timing summary
            self._print_timing_summary(candidate_schedules)

            if profiling_enabled:
                self._report_performance_metrics(worker_metrics, profile_json_path)

            return candidate_schedules[:top_n]

        finally:
            allow_sleep(sleep_handle)

    def _setup_rotation_callback(self, confirm_rotation_callback):
        """Use an explicit caller callback; backend code never prompts for input."""
        if confirm_rotation_callback is None:

            def confirm_rotation_callback() -> bool:
                """Default: never approve a relaxed-rotation retry."""
                return False

        return confirm_rotation_callback

    def _generate_weekend_variants(self, confirm_rotation_callback, weekend_variant_mode):
        """Generate weekend variants based on the selected fallback mode."""
        mode = self._normalize_weekend_variant_mode(weekend_variant_mode)

        if mode == self.WeekendVariantMode.STRICT_ONLY:
            variants = self.generate_all_weekend_variants(allow_rotation_violations=False)
            if not variants:
                logger.info("No feasible variants with strict rotation mode.")
            return variants

        if mode == self.WeekendVariantMode.RELAXED_ALLOWED:
            variants = self.generate_all_weekend_variants(allow_rotation_violations=True)
            if not variants:
                logger.error("No feasible variants with relaxed rotation mode.")
            return variants

        # STRICT_THEN_RELAXED
        variants = self.generate_all_weekend_variants(allow_rotation_violations=False)
        if not variants:
            if not confirm_rotation_callback():
                logger.info("User declined to allow rotation repeats – abort.")
                return []
            variants = self.generate_all_weekend_variants(allow_rotation_violations=True)
            if not variants:
                logger.error("Still no feasible variants with repeats allowed.")
                return []
        return variants

    @staticmethod
    def _resolve_worker_count(max_workers: int | None, variant_count: int) -> int:
        """Pool size for evaluating ``variant_count`` variants."""
        if max_workers is None:
            max_workers = default_worker_count()
        workers = max(1, min(max_workers, usable_cpu_count(), variant_count))
        logger.info("Evaluating %d variants on %d worker processes.", variant_count, workers)
        return workers

    def _evaluate_variants(self, variants, max_workers=None):
        """Evaluate all variants either in parallel or serially."""
        candidate_schedules: list = []
        workers = self._resolve_worker_count(max_workers, len(variants))

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                fut_map = {
                    pool.submit(_evaluate_variant_worker, (i, v, self.worker_tuning)): i
                    for i, v in enumerate(variants)
                }
                for fut in tqdm(
                    as_completed(fut_map),
                    total=len(fut_map),
                    desc="Evaluating variants",
                    unit="variant",
                ):
                    try:
                        candidate_schedules.append(fut.result())
                    except Exception as ex:
                        logger.error(f"Worker {fut_map[fut]} failed: {ex}")
        except Exception as e:
            # Fallback: run serially. Reset any partial results so a
            # mid-iteration pool failure does not leave duplicate idx entries.
            logger.warning(f"ProcessPool failed ({e}); evaluating serially.")
            candidate_schedules = []
            for idx, var in enumerate(variants):
                try:
                    candidate_schedules.append(
                        _evaluate_variant_worker((idx, var, self.worker_tuning))
                    )
                except Exception as ex:
                    logger.error(f"Serial worker {idx} failed: {ex}")

        return candidate_schedules

    def _evaluate_variants_with_profiling(self, variants, max_workers=None):
        """Evaluate all variants while collecting profiling metrics."""
        candidate_schedules: list = []
        all_worker_metrics: list[WorkerMetrics] = []
        workers = self._resolve_worker_count(max_workers, len(variants))

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                fut_map = {
                    pool.submit(_evaluate_variant_worker_profiled, (i, v, self.worker_tuning)): i
                    for i, v in enumerate(variants)
                }
                for fut in tqdm(
                    as_completed(fut_map),
                    total=len(fut_map),
                    desc="Evaluating variants",
                    unit="variant",
                ):
                    try:
                        idx, stats, nurse_counts, sched_df, metrics = fut.result()
                        candidate_schedules.append((idx, stats, nurse_counts, sched_df))
                        all_worker_metrics.append(metrics)
                    except Exception as ex:
                        logger.error(f"Worker {fut_map[fut]} failed: {ex}")
        except Exception as e:
            # Reset any partial results so a mid-iteration pool failure does
            # not leave duplicate idx entries.
            logger.warning(f"ProcessPool failed ({e}); evaluating serially with profiling.")
            candidate_schedules = []
            all_worker_metrics = []
            for idx, var in enumerate(variants):
                try:
                    result = _evaluate_variant_worker_profiled((idx, var, self.worker_tuning))
                    idx, stats, nurse_counts, sched_df, metrics = result
                    candidate_schedules.append((idx, stats, nurse_counts, sched_df))
                    all_worker_metrics.append(metrics)
                except Exception as ex:
                    logger.error(f"Serial worker {idx} failed: {ex}")

        return candidate_schedules, all_worker_metrics

    def _report_performance_metrics(
        self, worker_metrics: list[WorkerMetrics], output_path: str | os.PathLike[str] | None
    ) -> None:
        """Print and optionally export performance metrics."""
        if not worker_metrics:
            logger.info("Performance profiling collected no metrics to report.")
            return

        report = PerformanceReport(worker_metrics)
        report.print_summary()

        if output_path:
            try:
                report.export_json(os.fspath(output_path))
            except Exception as exc:
                logger.error(f"Failed to export performance metrics: {exc}")

    def _score_and_rank_variants(self, candidate_schedules):
        """Compute weighted scores and rank variants.

        Semantics alignment note:
        - ``BestStateTracker`` uses lexicographic ``ScheduleQuality`` comparison
          during intra-variant local search.
        - This method performs *inter-variant* final ranking by normalizing and
          weighting metrics. ``long_term`` uses the same ``_long_term_score``
          objective as ``ScheduleQuality.history_penalty`` (lower is better).
        """
        viol_counts = self.weekend_history.get_violation_counts()
        overage = self._historic_overage()
        weights = self.config.scoring_weights

        rows = []
        for idx, stats, nurse_counts, sched_df in candidate_schedules:
            rows.append(
                {
                    "idx": idx,
                    "rotation_rep": stats["rotation_rep"],
                    "gaps": stats["gaps"],
                    "rot_viol": self._rotation_violation_score(nurse_counts, viol_counts, sched_df),
                    "weekend_gap": self._weekend_gap_penalty(sched_df),
                    "balance": stats["balance_main"] + stats["balance_backup"],
                    "long_term": self._long_term_score(nurse_counts, overage),
                }
            )

        metric_df = weighted_scores_from_rows(rows, weights=weights)

        # Attach score back to stats dict
        for idx, stats, _, _ in candidate_schedules:
            stats["weighted_score"] = float(metric_df.loc[idx, "weighted_score"])

        # Rank by weighted score (lower = better)
        candidate_schedules.sort(key=lambda tpl: tpl[1]["weighted_score"])

    def _export_top_variants_as_pdfs(self, candidate_schedules, top_n):
        """Export the best variants as PDF files."""
        cal = calendar.Calendar(firstweekday=6)  # Sunday-first
        pdf_paths = []

        for rank, (_idx, _stats, _nurse_counts, sched_df) in enumerate(
            candidate_schedules[:top_n], start=1
        ):
            pdf_name = f"schedule_variant_{rank}.pdf"
            self._export_variant_pdf(pdf_name, sched_df, cal)
            pdf_paths.append(pdf_name)

        logger.info("Wrote %d PDF file(s): %s", len(pdf_paths), ", ".join(pdf_paths))

    def _print_timing_summary(self, candidate_schedules):
        """Print timing summary if enabled."""
        if MEASURE_PHASE_TIMES and candidate_schedules:
            n = len(candidate_schedules)
            phases = ["t_clone", "t_assign", "t_gapfill", "t_rebalance", "t_total"]
            labels = ["clone", "assign weekdays", "gap-fill", "rebalance", "TOTAL"]
            sums = [sum(cs[1].get(p, 0) for cs in candidate_schedules) for p in phases]

            summary = ", ".join(
                f"{lbl}={total / n:.4f}s" for lbl, total in zip(labels, sums, strict=True)
            )
            logger.info("Average phase times per variant: %s", summary)


__all__ = [
    "WorkerTuningConfig",
    "WORKER_TUNING",
    "NurseScheduler",
    "whole_weekend_range",
    "_evaluate_variant_worker",
    "_evaluate_variant_worker_profiled",
]
