"""Tests for the tunable plateau allowance and per-phase plateau reset.

The neutral-move (plateau) allowance is now driven by
``SchedulerConfig.max_plateau_depth`` and reset at the start of each
neutral-accepting rebalance phase, so a phase never inherits an exhausted
plateau counter from an earlier phase that shared the tracker.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from scheduler import (
    BestStateTracker,
    ScheduleState,
    ScheduleVariant,
    SchedulerConfig,
)


class _DummyNurseManager:
    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


def _build_variant(config: SchedulerConfig, nurses=("Alice", "Bob")) -> ScheduleVariant:
    idx = pd.date_range("2026-01-05", periods=5, freq="D")  # Mon-Fri
    schedule = pd.DataFrame(index=idx, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = [False, False, False, False, True]
    counts_idx = pd.Index(list(nurses))
    state = ScheduleState(
        schedule=schedule,
        main_assignment_counts=pd.Series(0, index=counts_idx),
        backup_assignment_counts=pd.Series(0, index=counts_idx),
        last_assignment={n: None for n in nurses},
        last_pattern={n: None for n in nurses},
        weekend_tracking={},
        nurse_weekend_lists={n: [] for n in nurses},
        rotation_repeats=0,
    )
    availability = pd.DataFrame(True, index=idx, columns=list(nurses))
    return ScheduleVariant(
        state=state,
        nurses=list(nurses),
        availability=availability,
        config=config,
        nurse_manager=_DummyNurseManager(),
        pre_scheduled={},
        console_debug=False,
    )


def test_config_max_plateau_depth_defaults_and_overrides():
    assert SchedulerConfig().max_plateau_depth == 10
    assert SchedulerConfig(max_plateau_depth=25).max_plateau_depth == 25
    # Negative values are clamped to zero.
    assert SchedulerConfig(max_plateau_depth=-3).max_plateau_depth == 0


def test_tracker_reads_max_plateau_depth_from_config():
    variant = _build_variant(SchedulerConfig(max_plateau_depth=3))
    tracker = BestStateTracker(variant)
    assert tracker._max_plateau_depth == 3


def test_rebalance_resets_plateau_on_phase_entry():
    variant = _build_variant(SchedulerConfig())
    tracker = BestStateTracker(variant)
    tracker.initialize()

    # Simulate an earlier phase that exhausted the plateau allowance.
    tracker._plateau_depth = tracker._max_plateau_depth

    # Entering the rebalance phase must hand it a fresh allowance. max_iterations=0
    # exercises only the phase setup (no actual rebalancing moves).
    variant.iterative_rebalance_no_revert(max_iterations=0, tracker=tracker)

    assert tracker._plateau_depth == 0
