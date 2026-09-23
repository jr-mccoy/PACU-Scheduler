"""Scheduling domain models, state tracking, and isolated search variants."""

from __future__ import annotations

import bisect
import datetime
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from functools import cached_property
from itertools import islice, permutations
from math import factorial
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
    Protocol,
)

import pandas as pd

from . import debug as _debug
from .assignment import AssignmentMutation, apply_assignment, revert_assignment
from .constraints import (
    WeekdayConstraintConfig,
    passes_basic_eligibility_checks,
    validate_post_weekend_assignment,
    validate_weekday_relative_to_weekend,
    validate_weekday_relative_to_weekend_gap,
)
from .generation import CandidateDomainBuilder, OrderGenerator
from .optimization import WindowRefillOptimizer
from .runtime import _count_main_backup_empties, is_empty
from .scoring import (
    QualityComparison,
    QualityMetrics,
    compare_quality,
    compute_quality_metrics,
    long_term_score,
)

if TYPE_CHECKING:
    from .engine import NurseScheduler

logger = logging.getLogger(__name__)

DAYS_IN_WEEKEND = 3
FRIDAY_WEEKDAY = 4
MONDAY_WEEKDAY = 0
TUESDAY_WEEKDAY = 1
WEDNESDAY_WEEKDAY = 2
THURSDAY_WEEKDAY = 3
DEFAULT_PRE_WEEKEND_WINDOW = 4
DEFAULT_POST_WEEKEND_WINDOW = 6
MAX_MAIN_ASSIGNMENTS_PER_WEEK = 1
MAX_TOTAL_ASSIGNMENTS_PER_WEEK = 2
# Weekend variants kept after each weekend's branching. Every survivor runs the
# full evaluation pipeline, so this is the main lever on run time; users tune
# it for their machine through the settings file.
DEFAULT_MAX_WEEKEND_VARIANTS = 1000
ANALYSE_INITIAL_WEEKDAY_GAPS = True
GAP_REPORT_FILE = "weekday_gap_report.txt"


class WeekendPattern(str, Enum):
    """Weekend shift patterns."""

    FSF = "FSF"  # First-Second-First (Main on Friday, Backup on Saturday, Main on Sunday)
    SFS = "SFS"  # Second-First-Second (Backup on Friday, Main on Saturday, Backup on Sunday)


class WeekendAssignment:
    """Represents a weekend assignment with specific patterns."""

    def __init__(self, date_obj, nurse_fsf: str, nurse_sfs: str):
        # Convert to pandas Timestamp and normalize to remove time component
        if not isinstance(date_obj, pd.Timestamp):
            self.start_date = pd.Timestamp(date_obj).normalize()
        else:
            self.start_date = date_obj.normalize()
        self.nurse_fsf = nurse_fsf  # Nurse with First-Second-First pattern
        self.nurse_sfs = nurse_sfs  # Nurse with Second-First-Second pattern

    def to_dict(self) -> dict:
        """Convert assignment to dictionary representation."""
        return {
            "date": self.start_date.strftime("%Y-%m-%d"),  # Format as YYYY-MM-DD
            "nurse_fsf": self.nurse_fsf,
            "nurse_sfs": self.nurse_sfs,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WeekendAssignment:
        """Create WeekendAssignment from dictionary representation."""
        return cls(pd.Timestamp(data["date"]), data["nurse_fsf"], data["nurse_sfs"])


class Role(str, Enum):
    """Nurse assignment roles."""

    MAIN = "main"
    BACKUP = "backup"


class NurseManagerProtocol(Protocol):
    """Protocol defining the nurse manager interface."""

    db_name: str
    nurses: dict[str, dict[str, Any]]

    def is_prn_nurse(self, nurse: str) -> bool: ...
    def is_late_shift_nurse(self, nurse: str) -> bool: ...


class WeekendHistoryProtocol(Protocol):
    """Protocol defining the weekend history interface."""

    def get_last_pattern(self, nurse: str) -> str | None: ...
    def get_weekends(self, nurse: str) -> list[pd.Timestamp]: ...


class PreSchedulerProtocol(Protocol):
    def get_assignments_in_range(
        self,
        start_date: str | datetime.date | pd.Timestamp,
        end_date: str | datetime.date | pd.Timestamp,
    ) -> dict[pd.Timestamp, dict[str, str | None]]: ...


class SchedulerConfig:
    def __init__(
        self,
        weekend_gap_days: int = 14,
        main_score_factor: int = 10,
        backup_score_factor: int = 10,
        availability_penalty: int = 10,
        min_days_between_assignments: int = 2,
        allow_post_weekend_wednesday_main: bool = False,
        allow_post_weekend_wednesday_backup: bool = True,
        allow_post_weekend_thursday_main: bool = True,
        allow_post_weekend_thursday_backup: bool = True,
        *,
        scoring_weights: dict[str, float] | None = None,
        # One-day weekday gap (Mon–Wed / Tue–Thu) relaxation.  The master
        # flag allows it for any pair of roles; the two narrower flags allow
        # it only when the two shifts are both BACKUP, or one MAIN and one
        # BACKUP.  Any of the three switches the relaxed fallback on.
        allow_one_day_weekday_gap: bool = False,
        allow_midweek_pair_backup_only: bool = False,
        allow_midweek_pair_mixed: bool = False,
        max_plateau_depth: int = 10,
        max_weekend_variants: int | None = DEFAULT_MAX_WEEKEND_VARIANTS,
        max_week_permutations: int | None = 200,
        **extra,
    ):

        if allow_one_day_weekday_gap is None:
            allow_one_day_weekday_gap = False
        else:
            allow_one_day_weekday_gap = bool(allow_one_day_weekday_gap)

        self.allow_one_day_weekday_gap = allow_one_day_weekday_gap
        self.allow_midweek_pair_backup_only = bool(allow_midweek_pair_backup_only)
        self.allow_midweek_pair_mixed = bool(allow_midweek_pair_mixed)
        # Neutral-move (plateau) allowance for local search; tune to trade
        # exploration depth against compute (best-so-far is always retained).
        self.max_plateau_depth = max(0, int(max_plateau_depth))
        # Beam cap on weekend variant branching: variants grow roughly as
        # (valid pairs)^(weekends), and every survivor runs the full heavy
        # evaluation pipeline. 0 means unlimited.
        if max_weekend_variants is None:
            max_weekend_variants = DEFAULT_MAX_WEEKEND_VARIANTS
        self.max_weekend_variants = max(0, int(max_weekend_variants))
        # Cap on the slot orderings tried when rebalancing one week. The
        # rebalance pass permutes a week's modifiable (date, role) slots and
        # keeps the first ordering that improves the spread; a full week has
        # ten such slots, so an exhaustive search is 10! = 3,628,800 orderings
        # and only terminates early when an improvement happens to exist.
        # Weeks that cannot be improved would otherwise run for hours. 0 means
        # unlimited (the original exhaustive behaviour).
        if max_week_permutations is None:
            max_week_permutations = 200
        self.max_week_permutations = max(0, int(max_week_permutations))
        self.weekend_gap_days = weekend_gap_days
        self.main_score_factor = main_score_factor
        self.backup_score_factor = backup_score_factor
        self.availability_penalty = availability_penalty
        self.min_days_between_assignments = min_days_between_assignments
        self.allow_post_weekend_wednesday_main = allow_post_weekend_wednesday_main
        self.allow_post_weekend_wednesday_backup = allow_post_weekend_wednesday_backup
        self.allow_post_weekend_thursday_main = allow_post_weekend_thursday_main
        self.allow_post_weekend_thursday_backup = allow_post_weekend_thursday_backup

        # ── per-metric weights for the composite schedule score ───
        default_weights = {
            "rotation_rep": 0.30,  # pattern repeat count
            "gaps": 0.20,  # weekday gap-fill penalty
            "rot_viol": 0.15,  # historic rotation violations
            "weekend_gap": 0.15,  # fairness of “time-since-last-wknd”
            "balance": 0.10,  # main/backup daily balance
            "long_term": 0.10,  # 30-day over/under utilisation
        }

        if scoring_weights is None:
            self.scoring_weights = default_weights.copy()
        else:
            merged = default_weights.copy()
            for k, v in scoring_weights.items():
                if k in merged:
                    merged[k] = max(0.0, float(v))  # clip negatives
            self.scoring_weights = merged

        # normalise so Σw == 1 (absolute scale irrelevant)
        total = sum(self.scoring_weights.values()) or 1.0
        for k in self.scoring_weights:
            self.scoring_weights[k] /= total

        # ── keep any additional keyword untouched (future proofing) ────
        for k, v in extra.items():
            setattr(self, k, v)

    @property
    def one_day_gap_enabled(self) -> bool:
        """True when any form of the one-day weekday gap relaxation is on."""
        return bool(
            self.allow_one_day_weekday_gap
            or getattr(self, "allow_midweek_pair_backup_only", False)
            or getattr(self, "allow_midweek_pair_mixed", False)
        )

    def one_day_gap_allows_roles(self, role_a: str | None, role_b: str | None) -> bool:
        """Whether a one-day gap between shifts in *role_a* and *role_b* is allowed.

        A role of None means the other shift's role is unknown (e.g. worked
        before the scheduling window); only the master flag covers that.
        """
        if self.allow_one_day_weekday_gap:
            return True
        if role_a is None or role_b is None:
            return False
        roles = {role_a, role_b}
        if roles == {"backup"}:
            return bool(getattr(self, "allow_midweek_pair_backup_only", False))
        if roles == {"main", "backup"}:
            return bool(getattr(self, "allow_midweek_pair_mixed", False))
        return False


class WeekBackup(NamedTuple):
    """
    Immutable snapshot of the four objects a week-permutation may have
    to roll back to.
    """

    rows: pd.DataFrame  # schedule slice - columns ["main","backup"]
    main_counts: pd.Series  # copy of main_assignment_counts
    backup_counts: pd.Series  # copy of backup_assignment_counts
    last_assignment: dict  # deepcopy of last_assignment


class Comparison(Enum):
    """Trinary comparison helpers used by the tracker."""

    BETTER = -1
    EQUAL = 0
    WORSE = 1


@dataclass(frozen=True)
class ScheduleQuality:
    """Unified quality metric capturing the primary optimisation goals."""

    total_gaps: int
    backup_spread: int
    main_spread: int
    total_spread: int
    rotation_penalty: int
    weekend_penalty: float
    history_penalty: float
    weighted_score: float = 0.0

    @classmethod
    def from_variant(
        cls,
        variant: ScheduleVariant,
        scheduler: NurseScheduler | None = None,
    ) -> ScheduleQuality:
        """Calculate the quality metrics from a schedule variant.

        NOTE:
        - ``compare_to`` uses these fields lexicographically for local-search
          accept/revert decisions in ``BestStateTracker``.
        - ``weighted_score`` is retained for reporting/debugging only; final
          candidate ranking is done by ``NurseScheduler._score_and_rank_variants``
          after metric normalization and weighting.
        """

        weekend_penalty = 0.0
        if scheduler and hasattr(scheduler, "_weekend_gap_penalty"):
            try:
                weekend_penalty = float(scheduler._weekend_gap_penalty(variant.state.schedule))
            except Exception:  # pragma: no cover - defensive guard
                weekend_penalty = 0.0

        history_penalty = 0.0
        if scheduler is not None and hasattr(scheduler, "_historic_overage"):
            overage = scheduler._historic_overage()
        else:
            overage = getattr(variant, "historic_overage", None)
        if overage:
            nurse_counts: dict[str, dict[str, int]] = {}
            for nurse in variant.state.main_assignment_counts.index:
                m = int(variant.state.main_assignment_counts.get(nurse, 0))
                b = int(variant.state.backup_assignment_counts.get(nurse, 0))
                nurse_counts[str(nurse)] = {"main": m, "backup": b, "total": m + b}
            history_penalty = float(long_term_score(nurse_counts, overage))

        weighted_score = variant._rebalance_score(alpha=1.0, beta=1.0)
        metrics = compute_quality_metrics(
            schedule_df=variant.state.schedule,
            main_counts=variant.state.main_assignment_counts,
            backup_counts=variant.state.backup_assignment_counts,
            rotation_repeats=int(getattr(variant.state, "rotation_repeats", 0)),
            count_gaps_fn=lambda df: variant.count_gaps(),
            weekend_penalty=weekend_penalty,
            history_penalty=history_penalty,
            weighted_score=weighted_score,
        )

        return cls(
            total_gaps=metrics.total_gaps,
            backup_spread=metrics.backup_spread,
            main_spread=metrics.main_spread,
            total_spread=metrics.total_spread,
            rotation_penalty=metrics.rotation_penalty,
            weekend_penalty=metrics.weekend_penalty,
            history_penalty=metrics.history_penalty,
            weighted_score=metrics.weighted_score,
        )

    def compare_to(self, other: ScheduleQuality, *, float_tol: float = 1e-6) -> Comparison:
        """Lexicographic comparison with tolerance for float metrics.

        This order is the tracker acceptance policy (earlier keys dominate):
        gaps -> spreads -> rotation -> weekend-gap penalty -> long-term history.
        """

        mine = QualityMetrics(
            total_gaps=self.total_gaps,
            backup_spread=self.backup_spread,
            main_spread=self.main_spread,
            total_spread=self.total_spread,
            rotation_penalty=self.rotation_penalty,
            weekend_penalty=self.weekend_penalty,
            history_penalty=self.history_penalty,
            weighted_score=self.weighted_score,
        )
        theirs = QualityMetrics(
            total_gaps=other.total_gaps,
            backup_spread=other.backup_spread,
            main_spread=other.main_spread,
            total_spread=other.total_spread,
            rotation_penalty=other.rotation_penalty,
            weekend_penalty=other.weekend_penalty,
            history_penalty=other.history_penalty,
            weighted_score=other.weighted_score,
        )
        result = compare_quality(mine, theirs, float_tol=float_tol)
        if result == QualityComparison.BETTER:
            return Comparison.BETTER
        if result == QualityComparison.WORSE:
            return Comparison.WORSE
        return Comparison.EQUAL

    def is_better_than(self, other: ScheduleQuality) -> bool:
        return self.compare_to(other) == Comparison.BETTER

    def is_at_least_as_good(self, other: ScheduleQuality) -> bool:
        return self.compare_to(other) in (Comparison.BETTER, Comparison.EQUAL)

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return (
            "Quality("
            f"gaps={self.total_gaps}, "
            f"spread=({self.backup_spread},{self.main_spread},{self.total_spread}), "
            f"rot={self.rotation_penalty}, "
            f"wknd={self.weekend_penalty:.3f}, "
            f"hist={self.history_penalty:.3f})"
        )


@dataclass
class StateSnapshot:
    """Complete snapshot of schedule state with index pinning."""

    quality: ScheduleQuality
    schedule_rows: pd.DataFrame
    schedule_index: pd.Index
    main_counts: pd.Series
    backup_counts: pd.Series
    last_assignment: dict
    rotation_repeats: int
    weekend_tracking: dict
    nurse_weekend_lists: dict
    last_pattern: dict
    index_hash: int

    @property
    def schedule(self) -> pd.DataFrame:
        """Backward-compatible alias for tests/debug code expecting ``snapshot.schedule``."""
        return self.schedule_rows.copy()

    @classmethod
    def capture(
        cls,
        variant: ScheduleVariant,
        quality: ScheduleQuality,
    ) -> StateSnapshot:
        sched = variant.state.schedule
        idx = sched.index.copy()

        return cls(
            quality=quality,
            schedule_rows=sched[["main", "backup"]].copy(),
            schedule_index=idx,
            main_counts=variant.state.main_assignment_counts.copy(),
            backup_counts=variant.state.backup_assignment_counts.copy(),
            last_assignment=dict(variant.state.last_assignment),
            rotation_repeats=variant.state.rotation_repeats,
            weekend_tracking=dict(variant.state.weekend_tracking),
            nurse_weekend_lists={k: list(v) for k, v in variant.state.nurse_weekend_lists.items()},
            last_pattern=dict(variant.state.last_pattern),
            index_hash=hash(tuple(idx)),
        )

    def restore_to(self, variant: ScheduleVariant) -> None:
        current_idx = variant.state.schedule.index
        current_hash = hash(tuple(current_idx))

        if current_hash != self.index_hash:
            raise ValueError(
                "Index mismatch: schedule index changed between snapshot and restore. "
                f"Expected hash {self.index_hash}, got {current_hash}"
            )

        variant.state.schedule.loc[self.schedule_index, ["main", "backup"]] = self.schedule_rows

        variant.state.main_assignment_counts = self.main_counts.copy()
        variant.state.backup_assignment_counts = self.backup_counts.copy()
        variant.state.last_assignment = dict(self.last_assignment)
        variant.state.rotation_repeats = self.rotation_repeats
        variant.state.weekend_tracking = dict(self.weekend_tracking)
        variant.state.nurse_weekend_lists = {
            k: list(v) for k, v in self.nurse_weekend_lists.items()
        }
        variant.state.last_pattern = dict(self.last_pattern)

        variant._invalidate_weekday_cache()


class BestStateTracker:
    """Manage best-state tracking and plateau-aware navigation."""

    def __init__(
        self,
        variant: ScheduleVariant,
        scheduler: NurseScheduler | None = None,
    ) -> None:
        self.variant = variant
        self.scheduler = scheduler

        self._global_best: StateSnapshot | None = None
        self._global_best_quality: ScheduleQuality | None = None

        self._iteration_snapshot: StateSnapshot | None = None
        self._iteration_quality: ScheduleQuality | None = None

        self._plateau_depth = 0
        # Plateau allowance is a tunable knob (SchedulerConfig.max_plateau_depth);
        # fall back to 10 if the variant carries no config.
        self._max_plateau_depth = getattr(getattr(variant, "config", None), "max_plateau_depth", 10)

        self._improvements = 0
        self._neutrals = 0
        self._reversions = 0

    def initialize(self) -> ScheduleQuality:
        self.variant._recalculate_assignment_counts()
        self.variant._update_last_assignment_dates()

        quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        snapshot = StateSnapshot.capture(self.variant, quality)

        self._global_best = snapshot
        self._global_best_quality = quality

        logger.debug("[Tracker] Initialized with: %s", quality)
        return quality

    def begin_iteration(self, phase_name: str = "") -> ScheduleQuality:
        # Counts are maintained incrementally by _inc_assign/_dec_assign and
        # restored exactly by StateSnapshot.restore_to(); skip expensive
        # full-schedule recalculation.
        quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        snapshot = StateSnapshot.capture(self.variant, quality)

        self._iteration_snapshot = snapshot
        self._iteration_quality = quality

        if phase_name:
            logger.debug("[Tracker] %s: Begin iteration with %s", phase_name, quality)

        return quality

    def evaluate_and_commit(
        self,
        *,
        phase_name: str = "",
        allow_neutral: bool = False,
    ) -> Comparison:
        if self._iteration_quality is None or self._iteration_snapshot is None:
            raise RuntimeError("Must call begin_iteration() before evaluate_and_commit().")

        # Counts are maintained incrementally; skip full recalculation.
        current_quality = ScheduleQuality.from_variant(self.variant, self.scheduler)
        # Local-search acceptance is *lexicographic* via ScheduleQuality.compare_to.
        # This is intentionally deterministic and stricter than the final
        # cross-variant weighted ranking used after search completes.
        comparison = current_quality.compare_to(self._iteration_quality)

        if comparison == Comparison.BETTER:
            self._improvements += 1
            self._plateau_depth = 0

            if self._global_best_quality is None or current_quality.is_better_than(
                self._global_best_quality
            ):
                self._global_best = StateSnapshot.capture(self.variant, current_quality)
                self._global_best_quality = current_quality
                if phase_name:
                    logger.debug("[Tracker] %s: NEW GLOBAL BEST: %s", phase_name, current_quality)
            else:
                if phase_name:
                    logger.debug(
                        "[Tracker] %s: Local improvement: %s -> %s",
                        phase_name,
                        self._iteration_quality,
                        current_quality,
                    )

            return Comparison.BETTER

        if comparison == Comparison.EQUAL and allow_neutral:
            if self._plateau_depth < self._max_plateau_depth:
                self._neutrals += 1
                self._plateau_depth += 1
                if phase_name:
                    logger.debug(
                        "[Tracker] %s: Neutral move accepted (plateau=%d): %s",
                        phase_name,
                        self._plateau_depth,
                        current_quality,
                    )
                return Comparison.EQUAL

            if phase_name:
                logger.debug("[Tracker] %s: Plateau limit reached, reverting", phase_name)
            self._revert_to_iteration()
            return Comparison.WORSE

        self._reversions += 1
        if phase_name:
            logger.debug("[Tracker] %s: No improvement, reverting: %s", phase_name, current_quality)
        self._revert_to_iteration()
        return Comparison.WORSE

    def _revert_to_iteration(self) -> None:
        if self._iteration_snapshot is None:
            raise RuntimeError("No iteration snapshot to revert to.")

        self._iteration_snapshot.restore_to(self.variant)

    def restore_global_best(self) -> None:
        if self._global_best is None:
            logger.warning("[Tracker] No global best to restore")
            return

        logger.debug("[Tracker] Restoring global best: %s", self._global_best_quality)
        self._global_best.restore_to(self.variant)

    def get_global_best_quality(self) -> ScheduleQuality | None:
        return self._global_best_quality

    def get_statistics(self) -> dict:
        return {
            "improvements": self._improvements,
            "neutrals": self._neutrals,
            "reversions": self._reversions,
            "plateau_depth": self._plateau_depth,
            "global_best": str(self._global_best_quality) if self._global_best_quality else None,
        }

    def reset_plateau(self) -> None:
        self._plateau_depth = 0


class ScheduleState:
    """
    Immutable snapshot that can be cloned for every search-tree branch.
    Deep-copies guarantee that no mutable object is ever shared across
    ScheduleVariants.
    """

    def __init__(
        self,
        schedule: pd.DataFrame,
        main_assignment_counts: pd.Series,
        backup_assignment_counts: pd.Series,
        last_assignment: dict,
        last_pattern: dict,
        weekend_tracking: dict,
        nurse_weekend_lists: dict | None = None,
        rotation_repeats: int = 0,
        pre_window_worked: dict | None = None,
        post_window_worked: dict | None = None,
    ):
        # Always own *private* copies of mutable objects
        self.schedule = schedule.copy()
        self.main_assignment_counts = main_assignment_counts.copy()
        self.backup_assignment_counts = backup_assignment_counts.copy()
        self.last_assignment = dict(last_assignment)
        self.last_pattern = dict(last_pattern)
        self.weekend_tracking = dict(weekend_tracking)

        # Per-nurse Friday lists
        if nurse_weekend_lists is None:
            self.nurse_weekend_lists = {n: [] for n in self.main_assignment_counts.index}
        else:
            self.nurse_weekend_lists = {k: list(v) for k, v in nurse_weekend_lists.items()}

        # Rotation-repeat counter used for variant ranking
        self.rotation_repeats = rotation_repeats

        # Days worked immediately before the scheduling window, per nurse
        # (weekend history plus persisted per-day schedule history). Read-only
        # after construction, so frozensets may be shared across clones.
        self.pre_window_worked = {k: frozenset(v) for k, v in (pre_window_worked or {}).items()}
        # The same for the days immediately after the window: shifts already
        # committed there (recorded weekends, pre-scheduled or applied days).
        self.post_window_worked = {k: frozenset(v) for k, v in (post_window_worked or {}).items()}

    def clone(self) -> ScheduleState:
        """
        Produce a fully detached copy. Every mutable member is either
        .copy() (for pandas objects) or deepcopy() (for plain Python
        containers), so no ScheduleVariant can mutate another one's data.
        """
        return ScheduleState(
            self.schedule.copy(),
            self.main_assignment_counts.copy(),
            self.backup_assignment_counts.copy(),
            dict(self.last_assignment),
            dict(self.last_pattern),
            dict(self.weekend_tracking),
            nurse_weekend_lists={k: list(v) for k, v in self.nurse_weekend_lists.items()},
            rotation_repeats=self.rotation_repeats,
            pre_window_worked=self.pre_window_worked,
            post_window_worked=self.post_window_worked,
        )


class ScheduleVariant:
    """
    One branch of the search tree with 100% isolation: every mutable object
    is privately owned, so no data leak can occur between variants.
    """

    def __init__(
        self,
        state: ScheduleState,
        nurses: list[str],
        availability: pd.DataFrame,
        config: SchedulerConfig,
        nurse_manager: NurseManagerProtocol,
        pre_scheduled: dict | None = None,
        historical_main: dict | None = None,
        historical_backup: dict | None = None,
        console_debug: bool | None = None,
        _skip_copy: bool = False,
        historic_overage: dict | None = None,
    ):
        # All *mutable* arguments are copied so the caller keeps ownership
        # _skip_copy=True is used by clone() to avoid redundant copies of read-only data
        self.state = state
        self.nurses = nurses if _skip_copy else nurses[:]
        self.availability = availability if _skip_copy else availability.copy()
        self.config = config
        self.nurse_manager = nurse_manager

        self._order_index = {n: i for i, n in enumerate(self.nurses)}
        self._weekday_counts_cache: dict[int, dict[str, int]] = {}
        self._total_counts_cache = None
        self._index_set = None

        # 30-day history used only for tie-breaking (read-only)
        if _skip_copy:
            self.hist_main = historical_main or {}
            self.hist_backup = historical_backup or {}
        else:
            self.hist_main = (historical_main or {}).copy()
            self.hist_backup = (historical_backup or {}).copy()

        # Each nurse's recent-history total minus the median (read-only). It
        # travels with the variant so worker processes can score long-term
        # fairness during local search without the scheduler object.
        self.historic_overage = dict(historic_overage or {})

        # Cache of late-shift staff
        self._late_set = {n for n in self.nurses if nurse_manager.is_late_shift_nurse(n)}

        # Immutable dict supplied by caller – we may share safely
        self.pre_scheduled = pre_scheduled or {}

        # Rotation-pattern repeats introduced on THIS branch of the search
        # tree, as (weekend_friday, nurse, pattern_value) tuples. Populated by
        # assign_weekend() and copied on clone(), so every final schedule
        # carries exactly the violations that belong to it.
        self.rotation_violations: list[tuple[pd.Timestamp, str, str]] = []

        if console_debug is None:
            # Opt-in: this traces every candidate slot considered, which is
            # thousands of lines per variant — useful when debugging the
            # search, unusable as default output.
            env_val = os.getenv("SCHEDULE_VARIANT_DEBUG", "0")
            try:
                self._console_debug = bool(int(env_val))
            except ValueError:
                self._console_debug = env_val.lower() in {"1", "true", "yes", "on"}
        else:
            self._console_debug = bool(console_debug)

        self.assignment_debug_logger = _debug.ASSIGNMENT_DEBUG_LOGGER
        self.Comparison = Comparison
        self.StateSnapshot = StateSnapshot
        self.BestStateTracker = BestStateTracker
        self.DEFAULT_POST_WEEKEND_WINDOW = DEFAULT_POST_WEEKEND_WINDOW
        self.DEFAULT_PRE_WEEKEND_WINDOW = DEFAULT_PRE_WEEKEND_WINDOW
        # NOTE: search helpers (domain_builder, order_generator, window_optimizer)
        # are constructed lazily via @cached_property below so they only hold a
        # reference to this variant through the VariantSearchContext surface,
        # not via an unconditional back-reference at __init__ time.

        self._initialize_pre_scheduled_slots()

    def _debug_print(self, msg: str, **kwargs) -> None:
        if self._console_debug:
            print(msg, **kwargs, flush=True)

    @staticmethod
    def is_empty(value) -> bool:
        return is_empty(value)

    # ------------------------------------------------------------------
    # VariantSearchContext public surface
    # ------------------------------------------------------------------
    # The optimizer/builder/ordering helpers depend on this stable public
    # surface (see scheduler/generation/context.py). Underscored aliases
    # are retained as internal call sites — they may be removed once all
    # internal references to the underscored names are migrated.

    @property
    def console_debug(self) -> bool:
        return self._console_debug

    @property
    def order_index(self) -> dict[str, int]:
        return self._order_index

    def debug_print(self, msg: str, **kwargs) -> None:
        return self._debug_print(msg, **kwargs)

    def is_pre_scheduled(self, date: pd.Timestamp, role: str) -> bool:
        return self._is_pre_scheduled(date, role)

    def eligible_domain(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self._eligible_domain(date, role, diagnostics, force_relaxed=force_relaxed)

    def eligible_domain_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self._eligible_domain_gap(date, role, diagnostics, force_relaxed=force_relaxed)

    def get_eligible_nurses_for_day(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        return self._get_eligible_nurses_for_day(
            date, role, diagnostics, relaxed_spacing=relaxed_spacing
        )

    def get_eligible_nurses_for_day_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        return self._get_eligible_nurses_for_day_gap(
            date, role, diagnostics, relaxed_spacing=relaxed_spacing
        )

    def inc_assign(
        self,
        date: pd.Timestamp,
        role: str,
        nurse: str,
        *,
        gap_phase: bool = False,
    ) -> bool:
        return self._inc_assign(date, role, nurse, gap_phase=gap_phase)

    def dec_assign(self, date: pd.Timestamp, role: str, nurse: str) -> None:
        return self._dec_assign(date, role, nurse)

    def spread_components(self) -> tuple[int, int, int]:
        return self._spread_components()

    def lexi_better(
        self,
        new_tuple: tuple[int, int, int],
        base_tuple: tuple[int, int, int],
    ) -> bool:
        return self._lexi_better(new_tuple, base_tuple)

    def collect_weekday_windows(self, window_weeks: int = 2) -> list[list[pd.Timestamp]]:
        return self._collect_weekday_windows(window_weeks=window_weeks)

    def build_window_varlist(self, days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, str]]:
        return self._build_window_varlist(days)

    def clear_window_assignments(self, days: list[pd.Timestamp]) -> None:
        return self._clear_window_assignments(days)

    def restore_from_backup(self, days: list[pd.Timestamp], backup) -> None:
        return self._restore_from_backup(days, backup)

    def get_all_weekdays(self) -> list[pd.Timestamp]:
        return self._get_all_weekdays()

    def gen_full_orders(
        self,
        days: list[pd.Timestamp],
        max_orders: int = 50,
    ) -> list[list[tuple[pd.Timestamp, str]]]:
        return self._gen_full_orders(days, max_orders=max_orders)

    def backtrack_full_order(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
    ) -> bool:
        return self._backtrack_full_order(vars_list, deadline, node_budget)

    def recalculate_assignment_counts(self) -> None:
        return self._recalculate_assignment_counts()

    def update_last_assignment_dates(self) -> None:
        return self._update_last_assignment_dates()

    def get_total_counts(self) -> pd.Series:
        return self._get_total_counts()

    def weekday_counts_for(self, weekday: int) -> dict[str, int]:
        return self._weekday_counts_for(weekday)

    def log_assignment_debug(self, **kwargs) -> None:
        return self._log_assignment_debug(**kwargs)

    # Search helpers — lazily constructed against the public context surface
    # rather than being held as eager back-references. This means a freshly
    # constructed variant does not unconditionally instantiate these helpers,
    # and helpers depend only on the `VariantSearchContext` protocol.
    @cached_property
    def domain_builder(self) -> CandidateDomainBuilder:
        return CandidateDomainBuilder(self)

    @cached_property
    def order_generator(self) -> OrderGenerator:
        return OrderGenerator(self)

    @cached_property
    def window_optimizer(self) -> WindowRefillOptimizer:
        return WindowRefillOptimizer(self)

    def _initialize_pre_scheduled_slots(self) -> None:
        """Initialize schedule with pre-scheduled assignments and update counters."""
        sched = self.state.schedule
        for day, slot in self.pre_scheduled.items():
            for role, nurse in slot.items():
                if nurse and is_empty(sched.at[day, role]):
                    sched.at[day, role] = nurse

        self._invalidate_weekday_cache()
        # Ensure counters & last-assignment dictionaries reflect the seeding
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

        # After placing pre-scheduled cells, ensure our per-nurse Friday lists
        # include any weekend already seeded (Fri/Sat/Sun).
        for friday in [d for d in sched.index if d.weekday() == FRIDAY_WEEKDAY]:
            weekend_days = [
                friday,
                friday + timedelta(days=1),
                friday + timedelta(days=2),
            ]
            for wd in weekend_days:
                if wd not in sched.index:
                    continue
                for role in ("main", "backup"):
                    n = sched.at[wd, role]
                    if n is None or is_empty(n):
                        continue
                    lst = self.state.nurse_weekend_lists.setdefault(n, [])
                    # Insert friday once per nurse per weekend (avoid duplicates)
                    pos = bisect.bisect_left(lst, friday)
                    if pos >= len(lst) or lst[pos] != friday:
                        bisect.insort(lst, friday)

    def clone(self) -> ScheduleVariant:
        """
        Return a *completely* detached copy of this variant with independent
        copies of all mutable state.  Read-only data (availability, hist,
        nurses, config, nurse_manager) is shared via _skip_copy=True.
        """
        new_variant = ScheduleVariant(
            state=self.state.clone(),
            nurses=self.nurses,
            availability=self.availability,
            config=self.config,
            nurse_manager=self.nurse_manager,
            pre_scheduled=self.pre_scheduled,
            historical_main=self.hist_main,
            historical_backup=self.hist_backup,
            console_debug=self._console_debug,
            _skip_copy=True,
            historic_overage=self.historic_overage,
        )
        new_variant.rotation_violations = list(self.rotation_violations)
        return new_variant

    def _is_pre_scheduled(self, date: pd.Timestamp, role: str) -> bool:
        """Returns True if the given date/role is pre-scheduled with a non-empty nurse name."""
        slot = self.pre_scheduled.get(date)
        nurse = slot.get(role) if slot else None
        return nurse is not None and str(nurse).strip() != ""

    # ===== CONFLICT DETECTION =====

    def _late_shift_conflict(self, nurse: str, date: pd.Timestamp, role: str) -> bool:
        """Check if assigning this nurse would create a late-shift conflict."""
        if nurse not in self._late_set:
            return False
        other_role = "backup" if role == "main" else "main"
        other_nurse = self.state.schedule.at[date, other_role]
        if other_nurse not in self._late_set:
            return False
        if self._is_pre_scheduled(date, role) and self._is_pre_scheduled(date, other_role):
            return False  # allow pre-scheduled late/late combinations
        return True

    # ===== WEEKEND ASSIGNMENT =====

    def assign_weekend(
        self,
        weekend_start: pd.Timestamp,
        fsf_nurse: str,
        sfs_nurse: str,
    ) -> None:
        """
        Write FSF/SFS pattern into schedule and update all counters.
        Counts every individual pattern repeat instead of lumping repeats together.
        """
        weekend_dates = self._get_weekend_dates(weekend_start)
        assignments = self._get_weekend_assignments(fsf_nurse, sfs_nurse)

        # Record pattern repeats against THIS branch before last_pattern is
        # overwritten, so violations stay attributable per final schedule.
        for nurse, new_pattern in (
            (fsf_nurse, WeekendPattern.FSF),
            (sfs_nurse, WeekendPattern.SFS),
        ):
            if self.state.last_pattern.get(nurse) == new_pattern:
                self.rotation_violations.append((weekend_start, nurse, new_pattern.value))

        self._apply_weekend_assignments(weekend_dates, assignments)
        self._update_weekend_tracking(weekend_start, fsf_nurse, sfs_nurse)
        self._update_nurse_weekend_lists(fsf_nurse, sfs_nurse, weekend_start)
        self._update_pattern_tracking(fsf_nurse, sfs_nurse)
        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    def _weekday_relaxation_applicable(self, nurse: str, date: pd.Timestamp) -> bool:
        """
        Relaxed (one-day) spacing is only allowed for Mon–Thu dates that are
        NOT in this nurse's immediate pre/post weekend windows.
        """
        if date.weekday() not in (0, 1, 2, 3):
            return False
        if self._is_in_pre_weekend_window(nurse, date):
            return False
        if self._is_in_post_weekend_window(nurse, date):
            return False
        return True

    def _spread_components(self) -> tuple[int, int, int]:
        """
        Return (backup_spread, main_spread, total_spread).
        Primary objective: minimize backup spread.
        """
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts
        tot = mains + backs
        s_b = int(backs.max() - backs.min()) if len(backs) else 0
        s_m = int(mains.max() - mains.min()) if len(mains) else 0
        s_t = int(tot.max() - tot.min()) if len(tot) else 0
        return (s_b, s_m, s_t)

    def _lexi_better(
        self, new_tuple: tuple[int, int, int], base_tuple: tuple[int, int, int]
    ) -> bool:
        """Return True if new_tuple is lexicographically better than base_tuple."""
        return new_tuple < base_tuple

    def _get_weekend_dates(self, weekend_start: pd.Timestamp) -> list[pd.Timestamp]:
        """Get the three dates of a weekend (Friday, Saturday, Sunday)."""
        return [
            weekend_start,
            weekend_start + timedelta(days=1),
            weekend_start + timedelta(days=2),
        ]

    def _get_weekend_assignments(self, fsf_nurse: str, sfs_nurse: str) -> list[tuple[str, str]]:
        """Get the main/backup assignments for a weekend pattern."""
        return [
            (fsf_nurse, sfs_nurse),  # Fri (FSF main)
            (sfs_nurse, fsf_nurse),  # Sat (SFS main)
            (fsf_nurse, sfs_nurse),  # Sun (FSF main)
        ]

    def _apply_weekend_assignments(
        self, weekend_dates: list[pd.Timestamp], assignments: list[tuple[str, str]]
    ) -> None:
        """Apply weekend assignments to schedule, respecting pre-scheduled slots."""
        for day, (main, backup) in zip(weekend_dates, assignments, strict=True):
            if is_empty(self.state.schedule.at[day, "main"]):
                self.state.schedule.at[day, "main"] = main
            if is_empty(self.state.schedule.at[day, "backup"]):
                self.state.schedule.at[day, "backup"] = backup

    def _update_weekend_tracking(
        self, weekend_start: pd.Timestamp, fsf_nurse: str, sfs_nurse: str
    ) -> None:
        """Update weekend tracking dictionary."""
        self.state.weekend_tracking[weekend_start] = (fsf_nurse, sfs_nurse)

    def _update_nurse_weekend_lists(
        self, fsf_nurse: str, sfs_nurse: str, weekend_start: pd.Timestamp
    ) -> None:
        """Keep each nurse's sorted Friday list up-to-date (deduplicated)."""
        for nurse in (fsf_nurse, sfs_nurse):
            lst = self.state.nurse_weekend_lists.setdefault(nurse, [])
            pos = bisect.bisect_left(lst, weekend_start)
            if pos >= len(lst) or lst[pos] != weekend_start:
                bisect.insort(lst, weekend_start)

    def _update_pattern_tracking(self, fsf_nurse: str, sfs_nurse: str) -> None:
        """Update pattern tracking and count repeats."""
        prev_fsf = self.state.last_pattern.get(fsf_nurse, None)
        prev_sfs = self.state.last_pattern.get(sfs_nurse, None)

        # Ensure keys exist to avoid KeyError or skipped counts
        self.state.last_pattern.setdefault(fsf_nurse, None)
        self.state.last_pattern.setdefault(sfs_nurse, None)

        self.state.last_pattern[fsf_nurse] = WeekendPattern.FSF
        self.state.last_pattern[sfs_nurse] = WeekendPattern.SFS

        self.state.rotation_repeats += int(prev_fsf == WeekendPattern.FSF) + int(
            prev_sfs == WeekendPattern.SFS
        )

    def _log_assignment_debug(
        self,
        *,
        context: str,
        phase: str,
        date: pd.Timestamp | None,
        role: str | None,
        eligible: Iterable[str] | None,
        diagnostics: dict[str, list[str]] | None,
        final_pick: str | None,
        note: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a structured assignment log entry when debug mode is enabled."""

        if not _debug.ASSIGNMENT_DEBUG_LOGGER.enabled:
            return

        eligible_list = list(eligible or [])
        diagnostics_map = {nurse: list(reasons) for nurse, reasons in (diagnostics or {}).items()}

        def _series_to_int_dict(series: pd.Series) -> dict[str, int]:
            if series is None:
                return {}
            out: dict[str, int] = {}
            for key, value in series.items():
                if pd.isna(value):
                    out[str(key)] = 0
                else:
                    try:
                        out[str(key)] = int(value)
                    except (TypeError, ValueError):
                        out[str(key)] = int(float(value) if value is not None else 0)
            return out

        main_counts = _series_to_int_dict(self.state.main_assignment_counts)
        backup_counts = _series_to_int_dict(self.state.backup_assignment_counts)
        total_series = self.state.main_assignment_counts + self.state.backup_assignment_counts
        total_counts = _series_to_int_dict(total_series)

        hist_main = {str(k): int(v) for k, v in self.hist_main.items()}
        hist_backup = {str(k): int(v) for k, v in self.hist_backup.items()}

        candidate_stats = [
            {
                "nurse": nurse,
                "main_assignments": main_counts.get(nurse, 0),
                "backup_assignments": backup_counts.get(nurse, 0),
                "total_assignments": total_counts.get(nurse, 0),
                "history_main": hist_main.get(nurse, 0),
                "history_backup": hist_backup.get(nurse, 0),
            }
            for nurse in eligible_list
        ]

        payload = {
            "context": context,
            "phase": phase,
            "date": date.date().isoformat() if isinstance(date, pd.Timestamp) else None,
            "role": role,
            "eligible": eligible_list,
            "candidate_stats": candidate_stats,
            "rejections": diagnostics_map,
            "counts_main": main_counts,
            "counts_backup": backup_counts,
            "counts_total": total_counts,
            "history_main": hist_main,
            "history_backup": hist_backup,
            "final_pick": final_pick,
            "note": note,
            "extra": dict(extra) if extra else {},
        }

        _debug.ASSIGNMENT_DEBUG_LOGGER.log(payload)

    # ===== WEEKDAY ASSIGNMENT =====

    def assign_weekdays(self) -> None:
        """Assign nurses to all Mon-Thu slots, skipping pre-scheduled slots."""
        sched = self.state.schedule
        weekday_dates = sched.index[~sched["is_weekend"]]

        for date in weekday_dates:
            self._debug_print(f"[ScheduleVariant] [WeekdayAssign] start {date.date()}")
            self._assign_roles_for_date(date)

        self._recalculate_assignment_counts()
        self._update_last_assignment_dates()

    def _assign_roles_for_date(self, date: pd.Timestamp) -> None:
        roles_and_counts = [
            ("main", self.state.main_assignment_counts),
            ("backup", self.state.backup_assignment_counts),
        ]
        for role, counts in roles_and_counts:
            if self._is_pre_scheduled(date, role):
                continue
            if not is_empty(self.state.schedule.at[date, role]):
                continue
            self._debug_print(f"[ScheduleVariant] [WeekdayAssign] {date.date()} role={role}")

            diag_map: dict[str, list[str]] | None = (
                {} if _debug.ASSIGNMENT_DEBUG_LOGGER.enabled else None
            )

            # 1) Base rules
            eligible = self._get_eligible_nurses_for_day(
                date,
                role,
                diagnostics=diag_map if diag_map is not None else None,
                relaxed_spacing=False,
            )

            used_relaxed = False

            # 2) Fallback to relaxed spacing ONLY if nothing is base-eligible
            if not eligible and self.config.one_day_gap_enabled:
                diag_relaxed: dict[str, list[str]] | None = (
                    {} if _debug.ASSIGNMENT_DEBUG_LOGGER.enabled else None
                )
                eligible = self._get_eligible_nurses_for_day(
                    date,
                    role,
                    diagnostics=diag_relaxed if diag_relaxed is not None else None,
                    relaxed_spacing=True,
                )
                if diag_relaxed is not None:
                    diag_map = diag_relaxed
                used_relaxed = True
                self._debug_print(
                    f"[ScheduleVariant] [WeekdayAssign] relaxed {date.date()} role={role}"
                )

            diag_for_log = diag_map or {}

            self._log_assignment_debug(
                context="weekday_assign",
                phase="pre_select",
                date=date,
                role=role,
                eligible=eligible,
                diagnostics=diag_for_log,
                final_pick=None,
                note="relaxed_spacing" if used_relaxed else None,
                extra={
                    "relaxed_spacing": used_relaxed,
                    "eligible_count": len(eligible),
                },
            )

            if not eligible:
                self._debug_print(
                    f"[ScheduleVariant] [WeekdayAssign] fail {date.date()} role={role}"
                )
                self.state.schedule.at[date, role] = None
                self._log_assignment_debug(
                    context="weekday_assign",
                    phase="post_select",
                    date=date,
                    role=role,
                    eligible=eligible,
                    diagnostics=diag_for_log,
                    final_pick=None,
                    note="no_candidate",
                    extra={
                        "relaxed_spacing": used_relaxed,
                        "eligible_count": 0,
                        "assignment_success": False,
                    },
                )
                continue

            pick = self._select_best_candidate(eligible, role, date)
            self.state.schedule.at[date, role] = pick
            counts[pick] += 1
            self.state.last_assignment[pick] = date
            self._invalidate_weekday_cache()

            self._log_assignment_debug(
                context="weekday_assign",
                phase="post_select",
                date=date,
                role=role,
                eligible=eligible,
                diagnostics=diag_for_log,
                final_pick=pick,
                note="relaxed_spacing" if used_relaxed else None,
                extra={
                    "relaxed_spacing": used_relaxed,
                    "eligible_count": len(eligible),
                    "assignment_success": True,
                },
            )

    def _get_eligible_nurses_for_day(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        sched = self.state.schedule
        avail = self.availability.loc[date]
        other_role = "backup" if role == "main" else "main"
        other_nurse = sched.at[date, other_role]

        out: list[str] = []
        for nurse in self.nurses:
            reasons: list[str] = []
            reasons_arg = reasons if diagnostics is not None else None

            if not self._passes_basic_eligibility_checks(
                nurse, date, role, avail, other_nurse, diagnostics=reasons_arg
            ):
                if diagnostics is not None:
                    diagnostics[nurse] = list(reasons)
                continue
            if not self._passes_advanced_eligibility_checks(
                nurse,
                date,
                role,
                relaxed_spacing=relaxed_spacing,
                diagnostics=reasons_arg,
            ):
                if diagnostics is not None:
                    diagnostics[nurse] = list(reasons)
                continue
            if diagnostics is not None:
                diagnostics[nurse] = list(reasons)
            out.append(nurse)
        return out

    # ────────────────────────────────────────────────────────────────────
    # 1.  BASIC ELIGIBILITY (fixes NaN availability handling)
    # ────────────────────────────────────────────────────────────────────
    def _passes_basic_eligibility_checks(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        avail: pd.Series,
        other_nurse: str,
        diagnostics: list[str] | None = None,
    ) -> bool:
        """
        Quick rejects before the heavier spacing / weekly-limit checks.

        * Treat **NaN / missing** cells in the availability matrix
          as **unavailable** (previously they slipped through).
        """
        return passes_basic_eligibility_checks(
            nurse=nurse,
            avail_row=avail,
            other_nurse=other_nurse,
            has_late_shift_conflict=self._late_shift_conflict(nurse, date, role),
            diagnostics=diagnostics,
        )

    def _passes_advanced_eligibility_checks(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        *,
        relaxed_spacing: bool = False,
        diagnostics: list[str] | None = None,
    ) -> bool:
        if not self._has_sufficient_spacing(nurse, date, role, relaxed_spacing=relaxed_spacing):
            if diagnostics is not None:
                diagnostics.append("insufficient_spacing")
            return False
        if not self._validate_weekly_assignment_limits(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekly_limit")
            return False
        if not self._validate_weekday_relative_to_weekend(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekend_window")
            return False
        return True

    # ────────────────────────────────────────────────────────────────────
    # 2.  FULL-ROW ELIGIBILITY (also fixes NaN handling)
    # ────────────────────────────────────────────────────────────────────
    def _is_nurse_eligible_for_assignment(
        self, nurse: str, date: pd.Timestamp, role: str, *, relaxed_spacing: bool = False
    ) -> bool:
        """Mon–Thu eligibility (NaN-safe availability + advanced rules)."""
        if self._late_shift_conflict(nurse, date, role):
            return False
        if date not in self.state.schedule.index:
            return False
        try:
            val = self.availability.at[date, nurse]
            if pd.isna(val) or not bool(val):
                return False
        except KeyError:
            return False
        return self._passes_advanced_eligibility_checks(
            nurse, date, role, relaxed_spacing=relaxed_spacing
        )

    def _is_nurse_eligible_for_assignment_gap(
        self,
        nurse: str,
        date: pd.Timestamp,
        role: str,
        *,
        relaxed_spacing: bool = False,
        diagnostics: list[str] | None = None,
    ) -> bool:
        """
        Gap-filling eligibility (NaN-safe) with Mon/Tue pre-weekend relaxation already
        in your code.  The 'relaxed_spacing' flag ONLY affects spacing distance,
        not the pre/post-weekend day-of-week allowances.
        """
        if self._late_shift_conflict(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("late_shift_conflict")
            return False
        if date not in self.state.schedule.index:
            if diagnostics is not None:
                diagnostics.append("date_out_of_range")
            return False
        try:
            val = self.availability.at[date, nurse]
            if pd.isna(val) or not bool(val):
                if diagnostics is not None:
                    diagnostics.append("unavailable")
                return False
        except KeyError:
            if diagnostics is not None:
                diagnostics.append("unavailable")
            return False

        if not self._has_sufficient_spacing(nurse, date, role, relaxed_spacing=relaxed_spacing):
            if diagnostics is not None:
                diagnostics.append("insufficient_spacing")
            return False
        if not self._validate_weekly_assignment_limits(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekly_limit")
            return False
        if not self._validate_weekday_relative_to_weekend_gap(nurse, date, role):
            if diagnostics is not None:
                diagnostics.append("weekend_window")
            return False
        return True

    # ===== ASSIGNMENT MANAGEMENT =====

    def _inc_assign(
        self, date: pd.Timestamp, role: str, nurse: str, gap_phase: bool = False
    ) -> bool:
        """Assign nurse to schedule and update counters (with relaxed fallback if needed)."""
        if self._is_pre_scheduled(date, role):
            return False

        # Pick the right checker
        check = (
            self._is_nurse_eligible_for_assignment_gap
            if gap_phase
            else self._is_nurse_eligible_for_assignment
        )

        # 1) Try base spacing first
        ok = check(nurse, date, role, relaxed_spacing=False)

        # 2) If base fails, optionally try relaxed spacing (only Mon–Thu and
        #    only when not in this nurse's pre/post weekend windows)
        if (
            not ok
            and self.config.one_day_gap_enabled
            and self._weekday_relaxation_applicable(nurse, date)
        ):
            ok = check(nurse, date, role, relaxed_spacing=True)

        if not ok:
            return False

        apply_assignment(
            schedule_df=self.state.schedule,
            main_counts=self.state.main_assignment_counts,
            backup_counts=self.state.backup_assignment_counts,
            last_assignment=self.state.last_assignment,
            date=date,
            role=role,
            nurse=nurse,
        )

        self._invalidate_weekday_cache()
        return True

    # ────────────────────────────────────────────────────────────────────
    # 4.  ASSIGNMENT REMOVAL (fixes negative counters)
    # ────────────────────────────────────────────────────────────────────
    def _dec_assign(self, date: pd.Timestamp, role: str, nurse: str) -> None:
        """
        Roll back an assignment and keep counters *non-negative*.
        """
        if self._is_pre_scheduled(date, role):
            return

        revert_assignment(
            schedule_df=self.state.schedule,
            main_counts=self.state.main_assignment_counts,
            backup_counts=self.state.backup_assignment_counts,
            last_assignment=self.state.last_assignment,
            mutation=AssignmentMutation(
                date=date,
                role=role,
                previous_nurse=None,
                next_nurse=nurse,
            ),
        )

        self._invalidate_weekday_cache()

    # ===== NURSE SELECTION =====

    def _select_best_candidate(
        self, nurses: list[str], role: str, date: pd.Timestamp | None = None
    ) -> str:
        """
        Deterministic tie-breaking among 'nurses' for 'role':
          1) fewest role-specific assignments (backup if role=='backup', main otherwise)
          1.5) FEWEST assignments on this same weekday (Mon–Thu) so far (soft preference)
          2) fewest total assignments (main+backup)
          3) fewest 30-day total (main + backup)
          4) first by canonical global order (self.nurses)
        """
        role_counts = (
            self.state.main_assignment_counts
            if role == "main"
            else self.state.backup_assignment_counts
        )
        total_counts = self._get_total_counts()

        # Optional weekday diversity counts (Mon–Thu only)
        if date is not None and date.weekday() in (0, 1, 2, 3):
            w_counts = self._weekday_counts_for(int(date.weekday()))
        else:
            w_counts = {}

        # 1) fewest role-specific count
        min_role = min(role_counts[n] for n in nurses)
        tier = [n for n in nurses if role_counts[n] == min_role]
        if len(tier) == 1:
            return tier[0]

        # 1.5) fewest same-weekday count (soft)
        if w_counts:
            min_dow = min(w_counts.get(n, 0) for n in tier)
            tier_dow = [n for n in tier if w_counts.get(n, 0) == min_dow]
        else:
            tier_dow = tier
        if len(tier_dow) == 1:
            return tier_dow[0]

        # 2) fewest total
        min_total = min(total_counts[n] for n in tier_dow)
        tier2 = [n for n in tier_dow if total_counts[n] == min_total]
        if len(tier2) == 1:
            return tier2[0]

        # 3) fewest 30-day history
        def past_total(n: str) -> int:
            return self.hist_main.get(n, 0) + self.hist_backup.get(n, 0)

        min_hist = min(past_total(n) for n in tier2)
        tier3 = [n for n in tier2 if past_total(n) == min_hist]
        if len(tier3) == 1:
            return tier3[0]

        # 4) deterministic fallback by canonical order
        order = self._order_index
        return min(tier3, key=lambda n: order.get(n, 1_000_000))

    # ===== VALIDATION METHODS =====

    def _has_sufficient_spacing(
        self, nurse: str, date: pd.Timestamp, role: str, *, relaxed_spacing: bool = False
    ) -> bool:
        """
        Base: require self.config.min_days_between_assignments days clear on
        both sides.  If relaxed_spacing is True AND the relaxation is applicable
        for this nurse/date, we enforce only a one-day clear buffer.
        """
        base = int(self.config.min_days_between_assignments)
        min_days_off = base
        relaxed = (
            relaxed_spacing
            and self.config.one_day_gap_enabled
            and self._weekday_relaxation_applicable(nurse, date)
        )

        if relaxed:
            # One-day buffer: forbid assignments on adjacent days only
            min_days_off = max(1, base - 1)

        idx_set = self._get_index_set()
        worked_outside_window = self._worked_outside_window(nurse)
        for offset in range(1, min_days_off + 1):
            for check_date in (date - timedelta(days=offset), date + timedelta(days=offset)):
                if check_date in idx_set:
                    if self._nurse_assigned_on_date(nurse, check_date):
                        return False
                elif check_date in worked_outside_window:
                    # Shifts just before the window (weekend or per-day
                    # history) or just after it (committed weekends,
                    # pre-scheduled or applied days) still count toward spacing.
                    return False

        if relaxed and min_days_off < base and not self.config.allow_one_day_weekday_gap:
            # Only the role-scoped midweek-pair relaxations are on: a shift at
            # the distance the relaxation newly allows is acceptable only for
            # the permitted pair of roles.
            for offset in range(min_days_off + 1, base + 1):
                for check_date in (date - timedelta(days=offset), date + timedelta(days=offset)):
                    if check_date in idx_set:
                        other_role = self._role_on_date(nurse, check_date)
                        if other_role and not self.config.one_day_gap_allows_roles(
                            role, other_role
                        ):
                            return False
                    elif check_date in worked_outside_window:
                        # Role unknown for shifts outside the window.
                        return False
        return True

    def _worked_outside_window(self, nurse: str) -> frozenset:
        """Days *nurse* works just outside the window, on either side."""
        before = self.state.pre_window_worked.get(nurse, frozenset())
        after = self.state.post_window_worked.get(nurse, frozenset())
        return before | after if after else before

    def _role_on_date(self, nurse: str, date: pd.Timestamp) -> str | None:
        """``"main"``/``"backup"`` if *nurse* works *date* in this schedule, else None."""
        sched = self.state.schedule
        if sched.at[date, "main"] == nurse:
            return "main"
        if sched.at[date, "backup"] == nurse:
            return "backup"
        return None

    def _nurse_assigned_on_date(self, nurse: str, date: pd.Timestamp) -> bool:
        """Check if nurse is assigned on specific date."""
        sched = self.state.schedule
        return nurse == sched.at[date, "main"] or nurse == sched.at[date, "backup"]

    def _validate_weekly_assignment_limits(self, nurse: str, date: pd.Timestamp, role: str) -> bool:
        """Check weekly assignment limits."""
        week_start = date - timedelta(days=date.weekday())  # Always Monday
        idx_set = self._get_index_set()
        sched = self.state.schedule

        main_count = 0
        backup_count = 0
        for i in range(4):  # Mon–Thu
            d = week_start + timedelta(days=i)
            if d == date or d not in idx_set:
                continue
            if sched.at[d, "main"] == nurse:
                main_count += 1
            if sched.at[d, "backup"] == nurse:
                backup_count += 1
        total_count = main_count + backup_count

        if role == "main" and main_count >= MAX_MAIN_ASSIGNMENTS_PER_WEEK:
            return False
        if total_count >= MAX_TOTAL_ASSIGNMENTS_PER_WEEK:
            return False

        return True

    def _validate_weekday_relative_to_weekend(
        self, nurse: str, date: pd.Timestamp, role: str
    ) -> bool:
        """Validate weekday assignments relative to weekend schedule."""
        cfg = WeekdayConstraintConfig(
            allow_post_weekend_wednesday_main=self.config.allow_post_weekend_wednesday_main,
            allow_post_weekend_wednesday_backup=self.config.allow_post_weekend_wednesday_backup,
            allow_post_weekend_thursday_main=self.config.allow_post_weekend_thursday_main,
            allow_post_weekend_thursday_backup=self.config.allow_post_weekend_thursday_backup,
        )
        return validate_weekday_relative_to_weekend(
            nurse=nurse,
            date=date,
            role=role,
            config=cfg,
            is_in_pre_weekend_window=self._is_in_pre_weekend_window,
            is_in_post_weekend_window=self._is_in_post_weekend_window,
        )

    def _validate_post_weekend_assignment(
        self, weekday: int, role: str, cfg: SchedulerConfig
    ) -> bool:
        """Validate post-weekend assignment based on day and role."""
        return validate_post_weekend_assignment(
            weekday=weekday,
            role=role,
            config=WeekdayConstraintConfig(
                allow_post_weekend_wednesday_main=cfg.allow_post_weekend_wednesday_main,
                allow_post_weekend_wednesday_backup=cfg.allow_post_weekend_wednesday_backup,
                allow_post_weekend_thursday_main=cfg.allow_post_weekend_thursday_main,
                allow_post_weekend_thursday_backup=cfg.allow_post_weekend_thursday_backup,
            ),
        )

    def _validate_weekday_relative_to_weekend_gap(
        self, nurse: str, date: pd.Timestamp, role: str
    ) -> bool:
        """Gap-filling specific weekend validation with relaxed Monday/Tuesday rule."""
        cfg = WeekdayConstraintConfig(
            allow_post_weekend_wednesday_main=self.config.allow_post_weekend_wednesday_main,
            allow_post_weekend_wednesday_backup=self.config.allow_post_weekend_wednesday_backup,
            allow_post_weekend_thursday_main=self.config.allow_post_weekend_thursday_main,
            allow_post_weekend_thursday_backup=self.config.allow_post_weekend_thursday_backup,
        )
        return validate_weekday_relative_to_weekend_gap(
            nurse=nurse,
            date=date,
            role=role,
            config=cfg,
            is_in_pre_weekend_window=self._is_in_pre_weekend_window,
            is_in_post_weekend_window=self._is_in_post_weekend_window,
        )

    # ===== WEEKEND WINDOW DETECTION =====

    def _get_neighboring_fridays(
        self, nurse: str, current_date: pd.Timestamp
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        """Get the previous and next Friday for a nurse."""
        fridays = self.state.nurse_weekend_lists.get(nurse, [])
        idx = bisect.bisect_left(fridays, current_date)
        prev_fri = fridays[idx - 1] if idx > 0 else None
        next_fri = fridays[idx] if idx < len(fridays) else None
        return prev_fri, next_fri

    def _is_in_pre_weekend_window(
        self, nurse: str, date: pd.Timestamp, window: int = DEFAULT_PRE_WEEKEND_WINDOW
    ) -> bool:
        """Check if date is in pre-weekend window."""
        _, next_fri = self._get_neighboring_fridays(nurse, date)
        if next_fri is None:
            return False
        days = (next_fri - date).days
        return 1 <= days <= window

    def _is_in_post_weekend_window(
        self, nurse: str, date: pd.Timestamp, window: int = DEFAULT_POST_WEEKEND_WINDOW
    ) -> bool:
        """Check if date is in post-weekend window."""
        prev_fri, _ = self._get_neighboring_fridays(nurse, date)
        if prev_fri is None:
            return False
        days = (date - prev_fri).days
        return 1 <= days <= window

    def _invalidate_weekday_cache(self) -> None:
        """Clear cached per-weekday assignment counts and total counts."""
        self._weekday_counts_cache.clear()
        self._total_counts_cache = None

    def _get_total_counts(self) -> pd.Series:
        """Return cached main+backup total counts (invalidated with weekday cache)."""
        if self._total_counts_cache is None:
            self._total_counts_cache = (
                self.state.main_assignment_counts + self.state.backup_assignment_counts
            )
        return self._total_counts_cache

    def _get_index_set(self) -> frozenset:
        """Return cached frozenset of schedule index dates for O(1) membership."""
        if not hasattr(self, "_index_set") or self._index_set is None:
            self._index_set = frozenset(self.state.schedule.index)
        return self._index_set

    def _weekday_counts_for(self, weekday: int) -> dict[str, int]:
        """
        Return total assignments per nurse on the given weekday (Mon–Thu) in the
        current schedule (main + backup). Used as a soft tie-breaker so we avoid
        giving the same nurse too many of the same weekday across the period.
        """
        if weekday not in (0, 1, 2, 3):  # only Mon–Thu
            return {}
        cached = self._weekday_counts_cache.get(weekday)
        if cached is not None:
            return cached

        sched = self.state.schedule
        sub = sched.loc[~sched["is_weekend"], ["main", "backup"]]
        sub = sub[sub.index.weekday == weekday]
        if sub.empty:
            self._weekday_counts_cache[weekday] = {}
            return {}
        m = sub["main"].value_counts()
        b = sub["backup"].value_counts()
        counts = m.add(b, fill_value=0).astype(int)
        result = counts.to_dict()
        self._weekday_counts_cache[weekday] = result
        return result

    # ===== SCHEDULE UTILITY METHODS =====

    def _recalculate_assignment_counts(self) -> None:
        """Recalculate assignment counts from current schedule."""
        self._invalidate_weekday_cache()
        mains = self.state.schedule["main"].value_counts()
        backups = self.state.schedule["backup"].value_counts()

        for nurse in self.state.main_assignment_counts.index:
            self.state.main_assignment_counts[nurse] = mains.get(nurse, 0)
            self.state.backup_assignment_counts[nurse] = backups.get(nurse, 0)

    def _update_last_assignment_dates(self) -> None:
        """Recompute last-assignment exactly from the schedule (can move backward)."""
        sched = self.state.schedule
        # Iterate over a stable list of keys in case callers mutate the dict elsewhere
        for nurse in list(self.state.last_assignment.keys()):
            assigned = sched.index[(sched["main"] == nurse) | (sched["backup"] == nurse)]
            self.state.last_assignment[nurse] = assigned.max() if len(assigned) else None

    def _days_to(self, start: pd.Timestamp, end: pd.Timestamp) -> int:
        """Return number of days between dates."""
        return (end - start).days

    # ===== CONVENIENCE METHODS =====

    def count_gaps(self) -> int:
        """Count empty slots in schedule."""
        return _count_main_backup_empties(self.state.schedule)

    def get_weekdays(self) -> list[pd.Timestamp]:
        """Get all weekday dates from schedule (cached; index never changes)."""
        if not hasattr(self, "_weekdays_cache") or self._weekdays_cache is None:
            self._weekdays_cache = [
                d for d in self.state.schedule.index if not self.state.schedule.at[d, "is_weekend"]
            ]
        return self._weekdays_cache

    def get_weeks(self) -> list[list[pd.Timestamp]]:
        """Get weekday dates grouped by week (cached; index never changes)."""
        if not hasattr(self, "_weeks_cache") or self._weeks_cache is None:
            weekdays = self.get_weekdays()
            weeks = []
            seen = set()

            for date in weekdays:
                week_start = date - timedelta(days=date.weekday())
                if week_start not in seen:
                    week_days = [
                        week_start + timedelta(days=i)
                        for i in range(4)
                        if (week_start + timedelta(days=i)) in weekdays
                    ]
                    weeks.append(week_days)
                    seen.add(week_start)

            self._weeks_cache = weeks
        return self._weeks_cache

    def calculate_imbalance(self) -> int:
        """Calculate total assignment imbalance across all nurses."""
        main_counts = self.state.main_assignment_counts.values
        backup_counts = self.state.backup_assignment_counts.values

        imbalance_main = (max(main_counts) - min(main_counts)) if len(main_counts) > 0 else 0
        imbalance_backup = (
            (max(backup_counts) - min(backup_counts)) if len(backup_counts) > 0 else 0
        )

        return imbalance_main + imbalance_backup

    def _rebalance_score(self, alpha: float = 0.5, beta: float = 1.5) -> float:
        """Calculate rebalancing objective score."""
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts

        main_spread = int(mains.max() - mains.min())
        backup_spread = int(backs.max() - backs.min())

        total = mains + backs
        total_spread = int(total.max() - total.min())

        return main_spread + beta * backup_spread + alpha * total_spread

    # ===== ASSIGNMENT METHODS =====

    def _assign_slot_sequence(
        self,
        slots: Iterable[tuple[pd.Timestamp, str]],
        *,
        gap_mode: bool = False,
        update_state: bool = True,
        allow_partial: bool = False,
        force_relaxed: bool = False,
    ) -> bool:
        """Assign nurses to an ordered sequence of ``(date, role)`` slots."""

        domain_fn: Callable[[pd.Timestamp, str], list[str]]
        if gap_mode:

            def domain_fn(d, r):
                return self._eligible_domain_gap(d, r, force_relaxed=force_relaxed)
        else:

            def domain_fn(d, r):
                return self._eligible_domain(d, r, force_relaxed=force_relaxed)

        had_failure = False

        for date, role in slots:
            if self._is_pre_scheduled(date, role):
                continue
            if not is_empty(self.state.schedule.at[date, role]):
                continue
            self._debug_print(f"[ScheduleVariant] [SlotSeq] slot {date.date()} role={role}")

            domain = domain_fn(date, role)
            if not domain:
                self._debug_print(
                    f"[ScheduleVariant] [SlotSeq] empty {date.date()} role={role} gap={gap_mode}"
                )
                if allow_partial:
                    had_failure = True
                    continue
                return False

            placed = False
            for nurse in domain:
                if self._inc_assign(date, role, nurse, gap_phase=gap_mode):
                    placed = True
                    break

            if not placed:
                self._debug_print(
                    f"[ScheduleVariant] [SlotSeq] fail {date.date()} role={role} gap={gap_mode}"
                )
                if allow_partial:
                    had_failure = True
                    continue
                return False

        if update_state:
            self._recalculate_assignment_counts()
            self._update_last_assignment_dates()
        return not had_failure

    def assign_nurses_to_weekdays(
        self, weekdays: list[pd.Timestamp], role: str, sorted_nurses: list[str] | None = None
    ) -> bool:
        """
        Assign nurses to the given weekdays for ``role`` using live counts.

        This method now simply builds a sequence of slots and defers to
        :meth:`_assign_slot_sequence`, preserving the dynamic candidate
        ordering used during weekday assignment.
        """
        slots = [(date, role) for date in weekdays]
        return self._assign_slot_sequence(slots)

    def _get_eligible_nurses_for_day_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        relaxed_spacing: bool = False,
    ) -> list[str]:
        other_role = "backup" if role == "main" else "main"
        other_nurse = self.state.schedule.at[date, other_role]
        capture = diagnostics
        created_local_diag = False
        if capture is None and _debug.ASSIGNMENT_DEBUG_LOGGER.enabled:
            capture = {}
            created_local_diag = True

        try:
            avail_row = self.availability.loc[date]
        except KeyError:
            avail_row = pd.Series(dtype="object")

        out: list[str] = []
        for nurse in self.nurses:
            reasons: list[str] = []
            reasons_arg = reasons if capture is not None else None

            if not self._passes_basic_eligibility_checks(
                nurse,
                date,
                role,
                avail_row,
                other_nurse,
                diagnostics=reasons_arg,
            ):
                if capture is not None:
                    capture[nurse] = list(reasons)
                continue

            if not self._has_sufficient_spacing(
                nurse,
                date,
                role,
                relaxed_spacing=relaxed_spacing,
            ):
                if capture is not None:
                    reasons.append("insufficient_spacing")
                    capture[nurse] = list(reasons)
                continue

            if not self._validate_weekly_assignment_limits(nurse, date, role):
                if capture is not None:
                    reasons.append("weekly_limit")
                    capture[nurse] = list(reasons)
                continue

            if not self._validate_weekday_relative_to_weekend_gap(nurse, date, role):
                if capture is not None:
                    reasons.append("weekend_window")
                    capture[nurse] = list(reasons)
                continue

            if capture is not None:
                capture[nurse] = list(reasons)
            out.append(nurse)
        if _debug.ASSIGNMENT_DEBUG_LOGGER.enabled:
            diag_for_log = capture or {}
            self._log_assignment_debug(
                context="gap_candidates",
                phase="candidate_pool",
                date=date,
                role=role,
                eligible=out,
                diagnostics=diag_for_log,
                final_pick=None,
                note="relaxed_spacing" if relaxed_spacing else None,
                extra={
                    "relaxed_spacing": relaxed_spacing,
                    "eligible_count": len(out),
                },
            )
        if created_local_diag:
            capture = None
        return out

    # ===== NEXT WEEKEND UTILITY =====

    def _get_next_weekend_dates(self, current_date: pd.Timestamp) -> list[pd.Timestamp]:
        """Get the dates of the next weekend."""
        days_ahead = (FRIDAY_WEEKDAY - current_date.weekday()) % 7
        friday = current_date + timedelta(days=days_ahead)
        return [friday + timedelta(days=i) for i in range(DAYS_IN_WEEKEND)]

    def _is_nurse_assigned_to_next_weekend(self, nurse: str, current_date: pd.Timestamp) -> bool:
        """Check if nurse is assigned to next weekend."""
        weekend_dates = self._get_next_weekend_dates(current_date)
        return any(
            d in self.state.schedule.index
            and (
                self.state.schedule.at[d, "main"] == nurse
                or self.state.schedule.at[d, "backup"] == nurse
            )
            for d in weekend_dates
        )

    # ===== BACKUP AND RESTORE =====

    def backup_week_assignments(self, week_days: list[pd.Timestamp]) -> WeekBackup:
        """Return a snapshot of week assignments for restoration."""
        return WeekBackup(
            rows=self.state.schedule.loc[week_days, ["main", "backup"]].copy(),
            main_counts=self.state.main_assignment_counts.copy(),
            backup_counts=self.state.backup_assignment_counts.copy(),
            last_assignment=dict(self.state.last_assignment),
        )

    # ===== REBALANCING =====

    def iterative_rebalance_no_revert(
        self,
        tolerance: int = 1,
        max_iterations: int = 6000,
        alpha: float = 1.0,
        beta: float = 1.0,
        # early_stop_spread: tuple[int, int] | None = (1, 1),
        early_stop_spread=None,
        tracker: BestStateTracker | None = None,
    ) -> bool:
        """
        Rebalance Monday-Thursday weeks in place. Early-stop when both spreads
        are <= early_stop_spread.
        """
        created_tracker = tracker is None
        if created_tracker:
            tracker = BestStateTracker(self)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.begin_iteration("[Rebalance]")

        improved = False

        # Give this phase its full neutral-move (plateau) allowance instead of
        # inheriting an exhausted counter from an earlier phase that shared
        # this tracker. Best-so-far is always retained, so a fresh allowance
        # only ever helps the search cross plateaus toward a better schedule.
        tracker.reset_plateau()

        if early_stop_spread:
            s_b, s_m, _ = self._spread_components()
            if s_b <= early_stop_spread[0] and s_m <= early_stop_spread[1]:
                return True

        for iteration in range(max_iterations):
            tracker.begin_iteration(f"[Rebalance] Iter {iteration}")

            schedule_changed = self._rebalance_all_weeks()

            comparison = tracker.evaluate_and_commit(
                phase_name=f"[Rebalance] Iter {iteration}",
                allow_neutral=True,
            )
            if comparison == Comparison.BETTER:
                improved = True

            if early_stop_spread:
                s_b, s_m, _ = self._spread_components()
                if s_b <= early_stop_spread[0] and s_m <= early_stop_spread[1]:
                    tracker.restore_global_best()
                    logger.debug("[Rebalance] Target reached at iter %d", iteration)
                    return True

            if not schedule_changed and comparison != Comparison.BETTER:
                logger.debug("[Rebalance] No progress at iter %d", iteration)
                break

        tracker.restore_global_best()
        final_quality = tracker.get_global_best_quality()

        if final_quality and initial_quality:
            improved = final_quality.is_better_than(initial_quality) or improved

        logger.info(
            "[Rebalance] Final: %s -> %s (improved=%s)",
            initial_quality,
            final_quality,
            bool(improved),
        )
        logger.debug("[Rebalance] Statistics: %s", tracker.get_statistics())

        return bool(improved)

    def _spread_main_backup(self) -> tuple[int, int]:
        """Return (spread_main, spread_backup)."""
        mains = self.state.main_assignment_counts
        backs = self.state.backup_assignment_counts
        s_m = int(mains.max() - mains.min()) if len(mains) else 0
        s_b = int(backs.max() - backs.min()) if len(backs) else 0
        return s_m, s_b

    def _collect_weekday_windows(self, window_weeks: int = 2) -> list[list[pd.Timestamp]]:
        """
        Build sliding windows of 'window_weeks' consecutive Monday–Thursday blocks.
        Returns a list of lists of dates (Mon–Thu per week, concatenated).
        """
        idx = self.state.schedule.index
        if len(idx) == 0:
            return []
        first = idx.min()
        last = idx.max()

        # first Monday >= first
        offset = (MONDAY_WEEKDAY - first.weekday()) % 7
        cur = first + timedelta(days=offset)

        windows: list[list[pd.Timestamp]] = []
        while cur <= last:
            days: list[pd.Timestamp] = []
            anchor = cur
            for _ in range(window_weeks):
                for i in range(4):  # Mon..Thu
                    d = anchor + timedelta(days=i)
                    if d in idx and not bool(self.state.schedule.at[d, "is_weekend"]):
                        days.append(d)
                anchor += timedelta(days=7)
            if len(days) >= 2:
                windows.append(days)
            cur += timedelta(days=7)
        return windows

    def _clear_window_assignments(self, days: list[pd.Timestamp]) -> None:
        """
        Clear non-pre-scheduled assignments for the given 'days' for both roles.
        Uses _dec_assign to keep counts/last_assignment consistent.
        """
        for d in days:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                nurse = self.state.schedule.at[d, role]
                if not is_empty(nurse):
                    self._dec_assign(d, role, nurse)

    def _eligible_domain(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self.domain_builder.eligible_domain(
            date,
            role,
            diagnostics=diagnostics,
            force_relaxed=force_relaxed,
            gap_mode=False,
        )

    def _eligible_domain_gap(
        self,
        date: pd.Timestamp,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        force_relaxed: bool = False,
    ) -> list[str]:
        return self.domain_builder.eligible_domain(
            date,
            role,
            diagnostics=diagnostics,
            force_relaxed=force_relaxed,
            gap_mode=True,
        )

    def _build_window_varlist(self, days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, str]]:
        """
        Variables to assign in the window: all (day, role) pairs that are empty and not pre-scheduled.
        """
        vars_list: list[tuple[pd.Timestamp, str]] = []
        for d in days:
            for role in ("main", "backup"):
                if not self._is_pre_scheduled(d, role):
                    val = self.state.schedule.at[d, role]
                    if is_empty(val):
                        vars_list.append((d, role))
        return vars_list

    def _backtrack_window(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
        *,
        gap_mode: bool = False,
        force_relaxed: bool = False,
        depth: int = 0,
    ) -> bool:
        return self.window_optimizer.backtrack_window(
            vars_list,
            deadline,
            node_budget,
            gap_mode=gap_mode,
            force_relaxed=force_relaxed,
            depth=depth,
        )

    def iterative_window_refill_rebalance(
        self,
        window_weeks: int = 3,
        max_passes: int = 6000,
        time_limit_ms: int = 800000,
        node_limit: int = 8000000,
        target_spread: tuple[int, int] | None = (1, 1),
        tracker: BestStateTracker | None = None,
    ) -> bool:
        return self.window_optimizer.iterative_window_refill_rebalance(
            window_weeks=window_weeks,
            max_passes=max_passes,
            time_limit_ms=time_limit_ms,
            node_limit=node_limit,
            target_spread=target_spread,
            tracker=tracker,
        )

    def _get_all_weekdays(self) -> list[pd.Timestamp]:
        """All Mon–Thu dates in the schedule period (non-weekend)."""
        idx = self.state.schedule.index
        return [d for d in idx if not bool(self.state.schedule.at[d, "is_weekend"])]

    def _build_full_varlist(
        self, days: list[pd.Timestamp], role_order: str = "MB"
    ) -> list[tuple[pd.Timestamp, str]]:
        """
        Build a variable list over the entire period's weekdays.
        role_order "MB" → assign MAIN then BACKUP for each day; "BM" → BACKUP then MAIN.
        Skips pre-scheduled cells that already have fixed nurses.
        """
        return self.order_generator.build_full_varlist(days, role_order=role_order)

    def _gen_full_orders(
        self,
        days: list[pd.Timestamp],
        max_orders: int = 50,
    ) -> list[list[tuple[pd.Timestamp, str]]]:
        return self.order_generator.gen_full_orders(days, max_orders=max_orders)

    def _backtrack_full_order(
        self,
        vars_list: list[tuple[pd.Timestamp, str]],
        deadline: float,
        node_budget: list[int],
    ) -> bool:
        """
        Backtracking that follows the given fixed variable order vars_list.
        Requires fully assigning all variables in vars_list for success.
        """
        n = len(vars_list)

        def dfs(idx: int) -> bool:
            if time.perf_counter() >= deadline or node_budget[0] <= 0:
                return False

            # advance to next unfilled
            while idx < n:
                d, r = vars_list[idx]
                val = self.state.schedule.at[d, r]
                if is_empty(val):
                    break
                idx += 1
            if idx >= n:
                return True

            d0, r0 = vars_list[idx]
            dom = self._eligible_domain(d0, r0)
            if not dom:
                return False  # must assign this var

            for nurse in dom:
                node_budget[0] -= 1
                if node_budget[0] <= 0 or time.perf_counter() >= deadline:
                    return False

                if not self._inc_assign(d0, r0, nurse):
                    continue

                # bounded forward check
                fail = False
                look_ahead = 8
                j = idx + 1
                steps = 0
                while j < n and steps < look_ahead:
                    dj, rj = vars_list[j]
                    if is_empty(self.state.schedule.at[dj, rj]):
                        if not self._eligible_domain(dj, rj):
                            fail = True
                            break
                        steps += 1
                    j += 1

                if not fail and dfs(idx + 1):
                    return True

                self._dec_assign(d0, r0, nurse)

            return False

        return dfs(0)

    def iterative_full_period_refill(
        self,
        max_orders: int = 10000,
        per_attempt_time_ms: int = 45000,
        per_attempt_nodes: int = 350000,
        target_spread: tuple[int, int] = (1, 1),
        required_spread: bool = True,
        tracker: BestStateTracker | None = None,
    ) -> bool:
        return self.window_optimizer.iterative_full_period_refill(
            max_orders=max_orders,
            per_attempt_time_ms=per_attempt_time_ms,
            per_attempt_nodes=per_attempt_nodes,
            target_spread=target_spread,
            required_spread=required_spread,
            tracker=tracker,
        )

    def _rebalance_all_weeks(self) -> bool:
        """Rebalance all Monday-Thursday weeks."""
        schedule_changed = False
        idx = self.state.schedule.index
        if len(idx) == 0:
            return False

        first = idx.min()
        last = idx.max()

        # Find the first Monday on/after the first date
        offset = (MONDAY_WEEKDAY - first.weekday()) % 7
        current = first + timedelta(days=offset)

        while current <= last:
            week_days = self._get_week_days(current)
            if week_days:
                changed = self._try_week_permutations_no_revert(week_days)
                schedule_changed |= changed
            current += timedelta(days=7)

        return schedule_changed

    def _get_week_days(self, monday: pd.Timestamp) -> list[pd.Timestamp]:
        """Get Monday–Thursday dates for a given Monday that exist in the index (non-weekend)."""
        idx = self.state.schedule.index
        sched = self.state.schedule
        days: list[pd.Timestamp] = []
        for i in range(4):  # Mon..Thu
            d = monday + timedelta(days=i)
            if d in idx and not bool(sched.at[d, "is_weekend"]):
                days.append(d)
        return days

    def _try_week_permutations_no_revert(self, week_days: list[pd.Timestamp]) -> bool:
        """
        Try all permutations of modifiable ``(date, role)`` slots for one week and
        keep lexicographic improvements.
        """

        if not week_days:
            return False

        week_start = min(week_days)
        offset = (FRIDAY_WEEKDAY - week_start.weekday()) % 7
        friday = week_start + timedelta(days=offset)
        friday_label = friday.date()

        slots = [
            (date, role)
            for date in week_days
            for role in ("main", "backup")
            if not self._is_pre_scheduled(date, role)
        ]
        if not slots:
            self._debug_print(f"[ScheduleVariant] [WeekPerms] friday={friday_label} no-slots")
            return False
        slot_tuple = tuple(slots)

        self._debug_print(
            f"[ScheduleVariant] [WeekPerms] start friday={friday_label} total_slots={len(slot_tuple)}"
        )

        original_state = self.backup_week_assignments(week_days)
        orig_tuple = self._spread_components()

        def run_attempt(force_relaxed: bool) -> bool:
            mode = "relaxed" if force_relaxed else "strict"
            self._restore_from_backup(week_days, original_state)
            self._clear_week_assignments(week_days)
            cleared_state = self.backup_week_assignments(week_days)

            best_state: WeekBackup | None = None
            best_tuple = orig_tuple
            improved = False

            perm_iterator = permutations(slot_tuple) if slot_tuple else [tuple()]
            perm_limit = getattr(self.config, "max_week_permutations", 0)
            if perm_limit:
                perm_iterator = islice(perm_iterator, perm_limit)
            truncated = False

            for idx, order in enumerate(perm_iterator, start=1):
                if perm_limit and idx == perm_limit:
                    truncated = True
                if idx == 1 or idx % 50 == 0:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} perm={idx} mode={mode}"
                    )

                self._restore_from_backup(week_days, cleared_state)

                if not self._assign_slot_sequence(order, force_relaxed=force_relaxed):
                    continue

                new_tuple = self._spread_components()
                if self._lexi_better(new_tuple, best_tuple):
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} improved {best_tuple}->{new_tuple} mode={mode}"
                    )
                    best_tuple = new_tuple
                    best_state = self.backup_week_assignments(week_days)
                    improved = True

                if improved and best_tuple[0] <= 1 and best_tuple[1] <= 1:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} early-stop perm={idx} mode={mode}"
                    )
                    break

                if improved:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerms] friday={friday_label} improvement found perm={idx} mode={mode}"
                    )
                    break

            if improved and best_state is not None:
                self._restore_from_backup(week_days, best_state)
                self._debug_print(
                    f"[ScheduleVariant] [WeekPerms] friday={friday_label} applied mode={mode}"
                )
                return True

            if truncated:
                # Say so rather than letting a capped search read as an
                # exhaustive one that found nothing.
                logger.debug(
                    "[WeekPerms] friday=%s mode=%s: no improvement within the first "
                    "%d of %d slot orderings (max_week_permutations)",
                    friday_label,
                    mode,
                    perm_limit,
                    factorial(len(slot_tuple)),
                )

            self._restore_from_backup(week_days, original_state)
            return False

        if run_attempt(force_relaxed=False):
            return True

        if self.config.one_day_gap_enabled and run_attempt(force_relaxed=True):
            return True

        self._debug_print(f"[ScheduleVariant] [WeekPerms] friday={friday_label} no-change")
        return False

    def _clear_week_assignments(self, week_days: list[pd.Timestamp]) -> None:
        """Clear modifiable assignments for a week."""
        for d in week_days:
            for role in ("main", "backup"):
                if self._is_pre_scheduled(d, role):
                    continue
                nurse = self.state.schedule.at[d, role]
                if not is_empty(nurse):
                    self._dec_assign(d, role, nurse)

    # ===== GAP FILLING =====

    def iterative_gap_fill_no_revert(
        self,
        max_iterations: int = 40,
        tracker: BestStateTracker | None = None,
    ) -> bool:
        """Iteratively fill weekday gaps using tracker-backed snapshots."""

        created_tracker = tracker is None
        if created_tracker:
            tracker = BestStateTracker(self)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.begin_iteration("[GapFill]")

        improved = False

        for pass_idx in range(1, max_iterations + 1):
            pass_quality = tracker.begin_iteration(f"[GapFill] Pass {pass_idx}")
            schedule_changed = False
            self._debug_print(
                f"[ScheduleVariant] [GapFill] pass={pass_idx} start total={pass_quality.total_gaps}"
            )

            for week_days in self.get_weeks():
                if not week_days:
                    continue
                week_label = f"{min(week_days).date()}-{max(week_days).date()}"
                week_gaps = self._count_week_gaps(week_days)
                self._debug_print(
                    f"[ScheduleVariant] [GapFill] pass={pass_idx} week={week_label} before={week_gaps}"
                )
                if week_gaps == 0:
                    continue

                remaining = self._try_week_gap_permutations_no_revert(week_days)
                if remaining < week_gaps:
                    schedule_changed = True
                self._debug_print(
                    f"[ScheduleVariant] [GapFill] pass={pass_idx} week={week_label} after={remaining}"
                )

            comparison = tracker.evaluate_and_commit(
                phase_name=f"[GapFill] Pass {pass_idx}",
                allow_neutral=False,
            )
            if comparison == Comparison.BETTER:
                improved = True

            current_gaps = self.count_gaps()
            self._debug_print(
                f"[ScheduleVariant] [GapFill] pass={pass_idx} end total={current_gaps}"
            )

            if current_gaps == 0:
                self._debug_print(f"[ScheduleVariant] [GapFill] complete pass={pass_idx}")
                tracker.restore_global_best()
                return True

            if comparison == Comparison.WORSE and not schedule_changed:
                self._debug_print(f"[ScheduleVariant] [GapFill] stalled pass={pass_idx}")
                break

        tracker.restore_global_best()
        final_quality = tracker.get_global_best_quality()

        if final_quality and initial_quality:
            improved = final_quality.total_gaps < initial_quality.total_gaps or improved

        logger.info(
            "[GapFill] Final: %s -> %s (improved=%s)",
            initial_quality,
            final_quality,
            bool(improved),
        )

        return bool(final_quality and final_quality.total_gaps == 0)

    def _count_week_gaps(self, week_days: list[pd.Timestamp]) -> int:
        """Count gaps in a specific week."""
        return sum(
            is_empty(self.state.schedule.at[d, r]) for d in week_days for r in ("main", "backup")
        )

    def _try_week_gap_permutations_no_revert(self, week_days: list[pd.Timestamp]) -> int:
        """Fill gaps for one week using heuristic search and return remaining gaps."""

        if not week_days:
            return 0

        ordered_days = sorted(week_days)
        week_start = ordered_days[0]
        offset = (FRIDAY_WEEKDAY - week_start.weekday()) % 7
        friday = week_start + timedelta(days=offset)
        friday_label = friday.date()
        original_state = self.backup_week_assignments(ordered_days)
        orig_gaps = self._count_week_gaps(ordered_days)
        self._debug_print(
            f"[ScheduleVariant] [GapPerms] start friday={friday_label} gaps={orig_gaps}"
        )

        if orig_gaps == 0:
            return 0

        # Clear modifiable cells so the search operates on a clean slate
        self._clear_week_assignments(ordered_days)

        self._debug_print(f"[ScheduleVariant] [GapPerms] friday={friday_label} perm=1")
        new_gaps = self._fill_week_with_permutation(ordered_days)

        if new_gaps < orig_gaps:
            # Counts already maintained by _inc_assign in _fill_week_with_permutation
            self._debug_print(
                f"[ScheduleVariant] [GapPerms] friday={friday_label} improved {orig_gaps}->{new_gaps}"
            )
            return new_gaps

        # Retry with forced relaxed domains if allowed and first attempt stalled
        if self.config.one_day_gap_enabled:
            self._restore_from_backup(ordered_days, original_state)
            self._clear_week_assignments(ordered_days)
            self._debug_print(f"[ScheduleVariant] [GapPerms] friday={friday_label} perm=relaxed")
            relaxed_gaps = self._fill_week_with_permutation(
                ordered_days,
                force_relaxed=True,
            )
            if relaxed_gaps < orig_gaps:
                # Counts already maintained by _inc_assign in _fill_week_with_permutation
                self._debug_print(
                    f"[ScheduleVariant] [GapPerms] friday={friday_label} relaxed-improved {orig_gaps}->{relaxed_gaps}"
                )
                return relaxed_gaps

        self._restore_from_backup(ordered_days, original_state)
        self._debug_print(f"[ScheduleVariant] [GapPerms] friday={friday_label} no-change")
        return orig_gaps

    def _restore_from_backup(self, week_days: list[pd.Timestamp], state: WeekBackup) -> None:
        """Restore week state from backup."""
        sched = self.state.schedule
        sched.loc[week_days, ["main", "backup"]] = state.rows
        self.state.main_assignment_counts = state.main_counts.copy()
        self.state.backup_assignment_counts = state.backup_counts.copy()
        self.state.last_assignment = dict(state.last_assignment)
        self._invalidate_weekday_cache()

    def _fill_week_with_permutation(
        self,
        week_days: list[pd.Timestamp],
        *,
        force_relaxed: bool = False,
    ) -> int:
        """Assign one week's empty slots using backtracking and heuristic ordering."""

        if not week_days:
            self._debug_print("[ScheduleVariant] [WeekPerm] empty-week")
            return 0

        slots = self._build_window_varlist(week_days)
        if not slots:
            self._debug_print(
                f"[ScheduleVariant] [WeekPerm] no-slots {min(week_days).date()}-{max(week_days).date()}"
            )
            return self._count_week_gaps(week_days)

        window_label = f"{min(week_days).date()}-{max(week_days).date()}"
        self._debug_print(f"[ScheduleVariant] [WeekPerm] start {window_label} slots={len(slots)}")

        cleared_state = self.backup_week_assignments(week_days)
        best_state: WeekBackup | None = None
        best_remaining = float("inf")

        slot_tuple = tuple(slots)
        perm_iterator = permutations(slot_tuple) if slot_tuple else [tuple()]

        for idx, order in enumerate(perm_iterator, start=1):
            if idx == 1 or idx % 50 == 0:
                self._debug_print(
                    f"[ScheduleVariant] [WeekPerm] permutation={idx} window={window_label}"
                )

            self._restore_from_backup(week_days, cleared_state)

            self._assign_slot_sequence(
                order,
                gap_mode=True,
                update_state=False,
                allow_partial=True,
                force_relaxed=force_relaxed,
            )

            remaining = self._count_week_gaps(week_days)
            if remaining < best_remaining:
                best_remaining = remaining
                best_state = self.backup_week_assignments(week_days)
                if best_remaining == 0:
                    self._debug_print(
                        f"[ScheduleVariant] [WeekPerm] perfect assignment window={window_label} perm={idx}"
                    )
                    break

        if best_state is not None:
            self._restore_from_backup(week_days, best_state)
            remaining = (
                best_remaining
                if best_remaining != float("inf")
                else self._count_week_gaps(week_days)
            )
        else:
            self._restore_from_backup(week_days, cleared_state)
            remaining = self._count_week_gaps(week_days)

        self._debug_print(
            f"[ScheduleVariant] [WeekPerm] exhaustive result {window_label} gaps={remaining}"
        )
        return remaining

    # ===== DEBUG METHODS =====

    def print_current_schedule_state(self) -> None:
        """Debug method to print current schedule state."""
        print("\n==== CURRENT SCHEDULE STATE ====")
        print(self.state.schedule[["main", "backup"]].to_string())

        print("\nAssignment Counts:")
        print(f"Main: {dict(self.state.main_assignment_counts)}")
        print(f"Backup: {dict(self.state.backup_assignment_counts)}")

        print("\nLast Assignment Dates:")
        last_assign_dict = {
            k: (v.date() if v is not None else None) for k, v in self.state.last_assignment.items()
        }
        print(last_assign_dict)

        print("\nLast Patterns:")
        print(self.state.last_pattern)

        print("\nWeekend Tracking:")
        weekend_tracking_dict = {
            k.date() if isinstance(k, pd.Timestamp) else k: v
            for k, v in self.state.weekend_tracking.items()
        }
        print(weekend_tracking_dict)
        print("=" * 50)


__all__ = [
    "DEFAULT_MAX_WEEKEND_VARIANTS",
    "WeekendPattern",
    "WeekendAssignment",
    "Role",
    "NurseManagerProtocol",
    "WeekendHistoryProtocol",
    "PreSchedulerProtocol",
    "SchedulerConfig",
    "WeekBackup",
    "Comparison",
    "ScheduleQuality",
    "StateSnapshot",
    "BestStateTracker",
    "ScheduleState",
    "ScheduleVariant",
]
