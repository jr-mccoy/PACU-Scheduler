"""Tests for the weekend-variant beam cap (SchedulerConfig.max_weekend_variants).

Weekend branching grows roughly as (valid pairs)^(weekends) and every surviving
variant later runs the full heavy evaluation pipeline, so generation must be
able to bound the beam. Pruning keeps the variants with the fewest rotation
repeats, the most even weekend spread, and the largest minimum weekend gap,
with deterministic (stable) tie-breaking.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from scheduler import NurseScheduler, SchedulerConfig


class _DummyNurseManager:
    db_name = ":memory:"

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse):
        return []


class _DummyPreScheduler:
    def get_assignments_in_range(self, start_date, end_date):
        return {}


class _StaticWeekendHistory:
    def get_last_weekend_before(self, nurse, before_date):
        return None

    def get_last_pattern(self, nurse):
        return None

    def get_weekends(self, nurse):
        return []

    def get_violation_counts(self):
        return {}


def _build_scheduler(config: SchedulerConfig) -> NurseScheduler:
    return NurseScheduler(
        start_date="2026-01-05",
        end_date="2026-01-18",
        nurses=["Alice", "Bob", "Cara", "Dan", "Eve", "Finn"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=_StaticWeekendHistory(),
        pre_scheduler=_DummyPreScheduler(),
        config=config,
    )


def test_config_max_weekend_variants_defaults_and_overrides():
    assert SchedulerConfig().max_weekend_variants == 500
    assert SchedulerConfig(max_weekend_variants=25).max_weekend_variants == 25
    # 0 means unlimited; negatives clamp to 0; None falls back to the default.
    assert SchedulerConfig(max_weekend_variants=0).max_weekend_variants == 0
    assert SchedulerConfig(max_weekend_variants=-5).max_weekend_variants == 0
    assert SchedulerConfig(max_weekend_variants=None).max_weekend_variants == 500


def test_generation_respects_beam_cap():
    unlimited = _build_scheduler(SchedulerConfig(max_weekend_variants=0))
    baseline = unlimited.generate_all_weekend_variants()
    assert len(baseline) > 20, "scenario must branch beyond the cap to be meaningful"

    capped = _build_scheduler(SchedulerConfig(max_weekend_variants=20))
    variants = capped.generate_all_weekend_variants()
    assert 0 < len(variants) <= 20

    # Every surviving variant is still a complete, feasible weekend assignment.
    weekends = capped._get_weekends()
    for var in variants:
        assert sorted(var.state.weekend_tracking.keys()) == sorted(weekends)


def test_beam_cap_pruning_is_deterministic():
    runs = []
    for _ in range(2):
        scheduler = _build_scheduler(SchedulerConfig(max_weekend_variants=15))
        variants = scheduler.generate_all_weekend_variants()
        runs.append([sorted(v.state.weekend_tracking.items()) for v in variants])
    assert runs[0] == runs[1]


def _fake_variant(nurse_weekend_lists, violations=()):
    return SimpleNamespace(
        state=SimpleNamespace(nurse_weekend_lists=nurse_weekend_lists),
        rotation_violations=list(violations),
    )


def test_prune_prefers_fewer_violations_then_balance_then_gap():
    scheduler = _build_scheduler(SchedulerConfig())
    scheduler.nurses = ["Alice", "Bob"]

    f1 = pd.Timestamp("2026-01-09")
    f2 = pd.Timestamp("2026-01-16")
    f3 = pd.Timestamp("2026-01-23")

    with_violation = _fake_variant(
        {"Alice": [f1, f3], "Bob": [f2]}, violations=[(f1, "Alice", "FSF")]
    )
    unbalanced = _fake_variant({"Alice": [f1, f2, f3], "Bob": []})
    tight_gap = _fake_variant({"Alice": [f1, f2], "Bob": [f3]})
    wide_gap = _fake_variant({"Alice": [f1, f3], "Bob": [f2]})

    kept = scheduler._prune_weekend_variants(
        [with_violation, unbalanced, tight_gap, wide_gap], 2, f3
    )
    # wide_gap wins outright; tight_gap beats the unbalanced variant and the
    # one carrying a rotation violation.
    assert kept == [wide_gap, tight_gap]
