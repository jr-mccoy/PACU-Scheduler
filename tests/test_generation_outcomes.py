"""A crash must never look like "no feasible schedule" (audit finding 7)."""

from __future__ import annotations

import pytest
from scheduling_fixtures import build_scheduler, seed_db


def _crash():
    raise KeyError("simulated malformed data")


@pytest.mark.xfail(strict=True, reason="audit #7: crashes read as infeasible")
def test_generation_reports_a_crash_as_an_error(tmp_path, monkeypatch):
    scheduler = build_scheduler(seed_db(tmp_path), "2026-11-02", "2026-11-29")
    monkeypatch.setattr(scheduler, "_get_weekends", _crash)

    result = scheduler.generate_weekend_candidates()

    assert result.status == "error"
    assert "simulated malformed data" in result.error


@pytest.mark.xfail(strict=True, reason="audit #7: a crash prompts for rotation repeats")
def test_generate_schedule_raises_on_a_crash_without_offering_relaxation(tmp_path, monkeypatch):
    scheduler = build_scheduler(seed_db(tmp_path), "2026-11-02", "2026-11-29")
    monkeypatch.setattr(scheduler, "_get_weekends", _crash)
    asked = []

    with pytest.raises(Exception, match="simulated malformed data"):
        scheduler.generate_schedule(confirm_rotation_callback=lambda: asked.append(1) or True)

    assert asked == []
