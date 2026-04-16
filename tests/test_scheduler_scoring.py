import pandas as pd

from scheduler.scoring import (
    QualityComparison,
    QualityMetrics,
    compare_quality,
    compute_quality_metrics,
    weighted_scores_from_rows,
)


def test_compute_quality_metrics_spreads_and_rotation_are_deterministic():
    idx = pd.date_range("2026-01-01", periods=2, freq="D")
    sched = pd.DataFrame(index=idx, data={"main": ["Alice", None], "backup": ["Bob", None]})
    main_counts = pd.Series({"Alice": 1, "Bob": 0})
    backup_counts = pd.Series({"Alice": 0, "Bob": 1})

    out = compute_quality_metrics(
        schedule_df=sched,
        main_counts=main_counts,
        backup_counts=backup_counts,
        rotation_repeats=2,
        count_gaps_fn=lambda df: int(df[["main", "backup"]].isna().sum().sum()),
    )
    assert out.total_gaps == 2
    assert out.rotation_penalty == 2
    assert out.total_spread == 0


def test_compare_quality_uses_lexicographic_order():
    a = QualityMetrics(0, 0, 1, 1, 0, 0.0, 0.0)
    b = QualityMetrics(1, 0, 0, 0, 0, 0.0, 0.0)
    assert compare_quality(a, b) == QualityComparison.BETTER


def test_weighted_scores_from_rows_normalizes_and_scores():
    df = weighted_scores_from_rows(
        [
            {"idx": 0, "rotation_rep": 2, "gaps": 1},
            {"idx": 1, "rotation_rep": 0, "gaps": 0},
        ],
        weights={"rotation_rep": 0.5, "gaps": 0.5},
    )
    assert float(df.loc[1, "weighted_score"]) < float(df.loc[0, "weighted_score"])
