"""Regression test for the serial-fallback duplication + ranking crash.

When the ProcessPool fails mid-iteration, ``candidate_schedules`` already
holds the futures that completed before the failure. The serial fallback must
reset that list before re-evaluating every variant; otherwise it produces
duplicate ``idx`` entries and ``_score_and_rank_variants`` crashes on
``float(Series)``.
"""

from __future__ import annotations

from concurrent.futures.process import BrokenProcessPool

import pandas as pd

import scheduler.engine as engine
from scheduler import NurseScheduler, SchedulerConfig


class _DummyNurseManager:
    db_name = ":memory:"

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
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


def _build_scheduler() -> NurseScheduler:
    return NurseScheduler(
        start_date="2026-01-05",
        end_date="2026-01-31",
        nurses=["Alice", "Bob"],
        prn_nurses=[],
        nurse_manager=_DummyNurseManager(),
        weekend_history=_StaticWeekendHistory(),
        pre_scheduler=_DummyPreScheduler(),
        config=SchedulerConfig(),
    )


def _fake_worker(pair):
    idx, _var = pair
    stats = {"rotation_rep": 0, "gaps": 0, "balance_main": 0, "balance_backup": 0}
    nurse_counts = {"Alice": {"total": 0}, "Bob": {"total": 0}}
    s_idx = pd.date_range("2026-01-05", periods=4, freq="D")  # Mon-Thu, no Fri
    sched_df = pd.DataFrame(index=s_idx, columns=["main", "backup"], data=None)
    return (idx, stats, nurse_counts, sched_df)


class _FakeFuture:
    def __init__(self, val):
        self._val = val

    def result(self):
        return self._val


class _FakePool:
    """Runs the worker eagerly on submit; the failure comes from as_completed."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, arg):
        return _FakeFuture(fn(arg))


def _make_mid_iteration_failure(fail_after: int):
    def fake_as_completed(fut_map):
        for i, fut in enumerate(fut_map):
            if i >= fail_after:
                raise BrokenProcessPool("simulated mid-iteration pool failure")
            yield fut

    return fake_as_completed


def test_serial_fallback_dedups_and_ranking_survives(monkeypatch):
    scheduler = _build_scheduler()
    variants = ["v0", "v1", "v2", "v3"]

    monkeypatch.setattr(engine, "_evaluate_variant_worker", _fake_worker)
    monkeypatch.setattr(engine, "ProcessPoolExecutor", _FakePool)
    # First two futures complete, then the pool breaks mid-iteration.
    monkeypatch.setattr(engine, "as_completed", _make_mid_iteration_failure(2))

    result = scheduler._evaluate_variants(variants, max_workers=4)

    idxs = [entry[0] for entry in result]
    assert sorted(idxs) == [0, 1, 2, 3]
    assert len(idxs) == len(set(idxs)), f"duplicate idx entries: {idxs}"

    # With a duplicated index this raised TypeError: float() argument ... Series.
    scheduler._score_and_rank_variants(result)
    assert all("weighted_score" in entry[1] for entry in result)


def test_profiled_serial_fallback_dedups(monkeypatch):
    scheduler = _build_scheduler()
    variants = ["v0", "v1", "v2", "v3"]

    def fake_worker_profiled(pair):
        idx, stats, nurse_counts, sched_df = _fake_worker(pair)
        return (idx, stats, nurse_counts, sched_df, None)  # metrics = None

    monkeypatch.setattr(engine, "_evaluate_variant_worker_profiled", fake_worker_profiled)
    monkeypatch.setattr(engine, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(engine, "as_completed", _make_mid_iteration_failure(2))

    result, metrics = scheduler._evaluate_variants_with_profiling(variants, max_workers=4)

    idxs = [entry[0] for entry in result]
    assert sorted(idxs) == [0, 1, 2, 3]
    assert len(idxs) == len(set(idxs)), f"duplicate idx entries: {idxs}"
