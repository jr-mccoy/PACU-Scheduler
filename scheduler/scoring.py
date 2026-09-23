"""Deterministic quality and weighted scoring helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class QualityComparison(Enum):
    BETTER = -1
    EQUAL = 0
    WORSE = 1


@dataclass(frozen=True)
class QualityMetrics:
    total_gaps: int
    backup_spread: int
    main_spread: int
    total_spread: int
    rotation_penalty: int
    weekend_penalty: float
    history_penalty: float
    weighted_score: float = 0.0


def long_term_score(nurse_counts: dict[str, dict[str, int]], overage: dict[str, int]) -> int:
    """
    Penalty for giving previously over-used nurses more than the minimum.

    For each nurse, ``max(0, overage[n] + total_n - min_total)``, summed.
    Lower is better: nurses who worked more than the median in the recent
    history window should land at or below this schedule's minimum total.
    """
    if not nurse_counts:
        return 0

    min_total = min(c["total"] for c in nurse_counts.values())
    return sum(
        max(0, overage.get(n, 0) + counts["total"] - min_total)
        for n, counts in nurse_counts.items()
    )


def compute_quality_metrics(
    *,
    schedule_df: pd.DataFrame,
    main_counts: pd.Series,
    backup_counts: pd.Series,
    rotation_repeats: int,
    count_gaps_fn: Callable[[pd.DataFrame], int],
    weekend_penalty: float = 0.0,
    history_penalty: float = 0.0,
    weighted_score: float = 0.0,
) -> QualityMetrics:
    """Compute normalized quality dimensions from plain structures."""
    total_gaps = int(count_gaps_fn(schedule_df))
    totals = main_counts + backup_counts

    backup_spread = int(backup_counts.max() - backup_counts.min()) if len(backup_counts) else 0
    main_spread = int(main_counts.max() - main_counts.min()) if len(main_counts) else 0
    total_spread = int(totals.max() - totals.min()) if len(totals) else 0

    return QualityMetrics(
        total_gaps=total_gaps,
        backup_spread=backup_spread,
        main_spread=main_spread,
        total_spread=total_spread,
        rotation_penalty=int(rotation_repeats),
        weekend_penalty=round(float(weekend_penalty), 6),
        history_penalty=round(float(history_penalty), 6),
        weighted_score=float(weighted_score),
    )


def compare_quality(
    lhs: QualityMetrics,
    rhs: QualityMetrics,
    *,
    float_tol: float = 1e-6,
) -> QualityComparison:
    """Lexicographic quality comparison used in local-search acceptance."""

    def float_cmp(a: float, b: float) -> int:
        if abs(a - b) < float_tol:
            return 0
        return -1 if a < b else 1

    comparisons = [
        lhs.total_gaps - rhs.total_gaps,
        lhs.backup_spread - rhs.backup_spread,
        lhs.main_spread - rhs.main_spread,
        lhs.total_spread - rhs.total_spread,
        lhs.rotation_penalty - rhs.rotation_penalty,
        float_cmp(lhs.weekend_penalty, rhs.weekend_penalty),
        float_cmp(lhs.history_penalty, rhs.history_penalty),
    ]

    for value in comparisons:
        if value < 0:
            return QualityComparison.BETTER
        if value > 0:
            return QualityComparison.WORSE
    return QualityComparison.EQUAL


def weighted_scores_from_rows(
    rows: Iterable[dict],
    *,
    weights: dict[str, float],
) -> pd.DataFrame:
    """Normalize rows by metric and compute weighted composite score."""
    metric_df = pd.DataFrame(list(rows)).set_index("idx")

    for col in weights:
        lo, hi = metric_df[col].min(), metric_df[col].max()
        metric_df[col] = 0.0 if hi == lo else (metric_df[col] - lo) / (hi - lo)

    for col, w in weights.items():
        metric_df[col] *= w
    metric_df["weighted_score"] = metric_df[list(weights)].sum(axis=1)
    return metric_df
