"""Immutable tuning values shared by every variant-evaluation path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerTuningConfig:
    """Immutable algorithm tuning shared by all worker evaluation paths."""

    gap_fill_iterations: int = 300
    # Per-week complete-fill search budget during gap filling (MRV
    # backtracking); a week that cannot be filled completely within it falls
    # back to the best of a capped sample of slot orderings.
    gap_fill_node_limit: int = 20_000
    gap_fill_time_limit_ms: int = 2_000
    rebalance_tolerance: int = 1
    rebalance_iterations: int = 1500
    rebalance_early_stop_spread: tuple[int, int] | None = None
    window_refill_weeks: int = 3
    window_refill_max_passes: int = 650
    window_refill_time_limit_ms: int = 800000
    window_refill_node_limit: int = 750000
    # None stops the refill passes only once every spread is at its proven
    # lower bound (ScheduleVariant.spread_lower_bounds). A (backup, main)
    # tuple stops them as soon as both spreads are at most those values,
    # which can leave a better schedule unfound.
    window_refill_target_spread: tuple[int, int] | None = None
    # gen_full_orders produces at most 54 distinct orderings, so a larger
    # value changes nothing; 54 says what the search actually does.
    full_period_max_orders: int = 54
    full_period_per_attempt_time_ms: int = 800000
    full_period_per_attempt_nodes: int = 1500000
    full_period_target_spread: tuple[int, int] | None = None
    # With proven lower bounds as the target, the full-period refill also
    # runs when both spreads are already within (1, 1) but above their
    # bounds, which it never did before. Its attempts can thrash there, and
    # the budgets above would let them run for hours, so these bound that
    # extra search: each attempt, and the whole pass. Above (1, 1) the pass
    # keeps the budgets above, as before.
    full_period_extra_attempt_time_ms: int = 2_000
    full_period_extra_time_ms: int = 20_000
    # How the weekdays are filled. "exact" lists every legal fill of each
    # week and lets OR-Tools CP-SAT choose the optimal combination
    # (scheduler.optimization.exact_weekdays); it falls back to "search", the
    # passes above, when OR-Tools is missing, when the spacing rule couples
    # weeks, or when a week has more than exact_max_fills_per_week fills.
    weekday_solver: str = "exact"
    # "balanced": fewest unfilled slots, then main + backup spread, then the
    # larger of the two, then total spread, then recent-history fairness.
    # "backup_first": the search's order (backup, main, total, history).
    exact_objective: str = "balanced"
    # "for_balance": the gap-filling rules' exception (a Tuesday right before
    # the nurse's worked weekend) may be used anywhere, as few times as give
    # the best spreads. "when_needed": only in a week that cannot be staffed
    # under the ordinary rules.
    exact_gap_rule_exceptions: str = "for_balance"
    exact_time_limit_ms: int = 20_000
    exact_max_fills_per_week: int = 200_000


WORKER_TUNING = WorkerTuningConfig()

__all__ = ["WorkerTuningConfig", "WORKER_TUNING"]
