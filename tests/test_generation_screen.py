"""Generation-screen outcomes against a real database in a temp directory.

Covers audit finding 3 (ending a run without applying must not touch
weekend history) and the GUI half of finding 7 (failures are errors).
"""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("PySide6.QtWidgets")

from scheduler import NurseManager, WeekendHistory, WeekendPattern  # noqa: E402


@pytest.fixture
def app_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    from ui.app_shell import App

    window = App()
    yield window
    window.close()


def _seed_override(db: str) -> None:
    nurses = NurseManager(db)
    for name in ("A", "B"):
        nurses.add_nurse(name)
    history = WeekendHistory(db)
    history.add_assignment(pd.Timestamp("2026-10-02"), "A", "B")
    history.set_last_pattern("A", WeekendPattern.SFS)  # manual override


@pytest.mark.parametrize("outcome", ["cancelled", "error", "no results"])
def test_ending_a_run_without_applying_keeps_manual_overrides(app_window, outcome):
    from ui.config import DB_NAME

    _seed_override(DB_NAME)
    screen = app_window.generate
    screen._on_generate()  # opens the rotation dialog, as the button does

    if outcome == "cancelled":
        screen._on_worker_cancelled()
    elif outcome == "error":
        screen._on_worker_error("Traceback: simulated")
    else:
        screen._on_worker_finished([], None, None)

    assert WeekendHistory(DB_NAME).get_last_pattern("A") == WeekendPattern.SFS


# ── the generation worker ─────────────────────────────────────────────────
def _run_worker(tmp_path, monkeypatch, end="2026-11-15"):
    """Run ScheduleProgressWorker synchronously; return (errors, finished)."""
    from scheduling_fixtures import seed_db

    from scheduler import SharedSettings
    from ui.worker_threads import DB_NAME, ScheduleProgressWorker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NSCHED_FORCE_THREAD_POOL", "1")  # keep patches in-process
    seed_db(tmp_path, filename=DB_NAME)
    settings = dict(SharedSettings.DEFAULTS, weekend_gap_days=14, max_weekend_variants=4)

    worker = ScheduleProgressWorker("2026-11-02", end, settings=settings)
    errors, finished = [], []
    worker.error.connect(errors.append)
    worker.finished.connect(lambda *args: finished.append(args))
    worker.run()
    return errors, finished


def test_worker_generates_and_ranks_options(qapp, tmp_path, monkeypatch):
    import scheduler.engine as engine
    from scheduler import WorkerTuningConfig

    quick = WorkerTuningConfig(
        gap_fill_iterations=5,
        rebalance_iterations=5,
        window_refill_max_passes=2,
        window_refill_time_limit_ms=500,
        window_refill_node_limit=5_000,
        full_period_max_orders=2,
        full_period_per_attempt_time_ms=500,
        full_period_per_attempt_nodes=5_000,
    )
    monkeypatch.setattr(engine, "WORKER_TUNING", quick)

    errors, finished = _run_worker(tmp_path, monkeypatch, end="2026-11-08")

    assert errors == []
    (options, _scheduler, _history) = finished[0]
    assert options
    scores = [stats["weighted_score"] for _, stats, _, _ in options]
    assert scores == sorted(scores)


def test_worker_reports_a_generation_crash_as_an_error(qapp, tmp_path, monkeypatch):
    from scheduler import NurseScheduler

    def crash(self):
        raise KeyError("simulated malformed data")

    monkeypatch.setattr(NurseScheduler, "_get_weekends", crash)

    errors, finished = _run_worker(tmp_path, monkeypatch)

    assert finished == []
    assert errors and "simulated malformed data" in errors[0]


def test_worker_reports_an_error_when_no_variant_evaluates(qapp, tmp_path, monkeypatch):
    import ui.worker_threads as worker_threads

    def crash(args):
        raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(worker_threads, "_evaluate_variant_worker", crash)

    errors, finished = _run_worker(tmp_path, monkeypatch)

    assert finished == []
    assert errors and "simulated evaluation failure" in errors[0]
