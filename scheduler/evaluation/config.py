"""Immutable tuning values shared by every variant-evaluation path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerTuningConfig:
    """Immutable algorithm tuning shared by all worker evaluation paths."""

    gap_fill_iterations: int = 300
    rebalance_tolerance: int = 1
    rebalance_iterations: int = 1500
    rebalance_early_stop_spread: tuple[int, int] | None = None
    window_refill_weeks: int = 3
    window_refill_max_passes: int = 650
    window_refill_time_limit_ms: int = 800000
    window_refill_node_limit: int = 750000
    window_refill_target_spread: tuple[int, int] = (1, 1)
    full_period_max_orders: int = 1000
    full_period_per_attempt_time_ms: int = 800000
    full_period_per_attempt_nodes: int = 1500000
    full_period_target_spread: tuple[int, int] = (1, 1)


WORKER_TUNING = WorkerTuningConfig()

__all__ = ["WorkerTuningConfig", "WORKER_TUNING"]
