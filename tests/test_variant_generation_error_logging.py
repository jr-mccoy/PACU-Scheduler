"""Regression test for exception swallowing in generate_all_weekend_variants.

A genuine crash (e.g. KeyError, malformed data) used to be indistinguishable
from "no feasible schedule": the traceback went only to the debug sink, which
is off unless NSCHED_DEBUG is set. The traceback must now be logged via
logger.error unconditionally.
"""

from __future__ import annotations

import logging

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


def _build_scheduler():
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


def test_generate_variants_logs_traceback_on_crash(monkeypatch, caplog):
    scheduler = _build_scheduler()

    def boom():
        raise KeyError("simulated malformed data")

    monkeypatch.setattr(scheduler, "_get_weekends", boom)

    with caplog.at_level(logging.ERROR, logger="scheduler.engine"):
        result = scheduler.generate_all_weekend_variants()

    assert result == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "a genuine crash must be logged at ERROR level"
    combined = "\n".join(r.getMessage() for r in error_records)
    assert "KeyError" in combined
    assert "simulated malformed data" in combined
