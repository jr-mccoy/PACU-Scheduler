"""Regression tests: a ``ScheduleVariant`` must survive a process boundary.

Variants are handed to worker processes through a ``ProcessPoolExecutor``,
which pickles each work item. Two attributes used to make that impossible:
``self.pd = pd`` held a module object, and ``assignment_debug_logger`` held
the open JSONL/CSV file handles of the shared debug logger. Every worker
died with ``cannot pickle ...``; the engine caught each failure per-future,
logged it, and returned no candidate schedules — so schedule generation
silently produced nothing at all.
"""

from __future__ import annotations

import pickle

import pandas as pd

from scheduler import SchedulerConfig
from scheduler.debug import AssignmentDebugLogger
from scheduler.domain import ScheduleState, ScheduleVariant

NURSES = ("Alice", "Bob")


class _DummyNurseManager:
    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


def _build_variant() -> ScheduleVariant:
    index = pd.date_range("2026-01-05", periods=7, freq="D")
    schedule = pd.DataFrame(index=index, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = [day.weekday() >= 4 for day in index]
    counts_idx = pd.Index(list(NURSES))
    state = ScheduleState(
        schedule=schedule,
        main_assignment_counts=pd.Series(0, index=counts_idx),
        backup_assignment_counts=pd.Series(0, index=counts_idx),
        last_assignment=dict.fromkeys(NURSES),
        last_pattern=dict.fromkeys(NURSES),
        weekend_tracking={},
        nurse_weekend_lists={n: [] for n in NURSES},
        rotation_repeats=0,
    )
    return ScheduleVariant(
        state=state,
        nurses=list(NURSES),
        availability=pd.DataFrame(True, index=index, columns=list(NURSES)),
        config=SchedulerConfig(),
        nurse_manager=_DummyNurseManager(),
        pre_scheduled={},
        console_debug=False,
    )


def test_variant_round_trips_through_pickle():
    """The whole variant must pickle — this is what the worker pool does."""
    variant = _build_variant()

    restored = pickle.loads(pickle.dumps(variant))

    assert list(restored.nurses) == list(NURSES)
    assert restored.state.schedule.shape == variant.state.schedule.shape


def test_variant_does_not_carry_a_module_reference():
    """A module attribute is unpicklable and nothing reads one off the variant."""
    variant = _build_variant()

    assert not hasattr(variant, "pd")


def test_enabled_debug_logger_pickles_and_arrives_disabled(tmp_path):
    """An enabled logger holds open files; pickling must drop them, not fail."""
    logger = AssignmentDebugLogger(enabled=True, directory=tmp_path)
    assert logger.enabled, "logger should be writing files before it is pickled"

    restored = pickle.loads(pickle.dumps(logger))

    # Handles cannot cross the boundary, so the copy must not claim to be live.
    assert not restored.enabled
    assert restored._json_handle is None
    assert restored._csv_handle is None
    assert restored._csv_writer is None
    # It still has to behave: log() on a disabled logger is a no-op, not a crash.
    restored.log({"context": "after-unpickle"})

    logger.close()
