"""One generation pipeline for the GUI, the CLI and scripts (audit finding 13).

Evaluation is replaced by a fast deterministic fake so these tests cover the
orchestration (stages, progress, cancellation, ranking, parity between the
front ends), not the search itself.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from scheduling_fixtures import seed_db

import scheduler.engine as engine
from scheduler import (
    NurseManager,
    PreScheduler,
    SharedSettings,
    WeekendHistory,
    build_scheduler_from_settings,
)

SETTINGS = dict(SharedSettings.DEFAULTS, weekend_gap_days=14, max_weekend_variants=6)
START, END = "2026-11-02", "2026-11-15"


def _fake_worker(item):
    idx, variant, _tuning = item
    counts = {n: {"main": 0, "backup": 0, "total": 0} for n in variant.nurses}
    stats = {"rotation_rep": 0, "gaps": idx % 3, "balance_main": idx % 2, "balance_backup": 1}
    return idx, stats, counts, variant.state.schedule.copy()


@pytest.fixture
def fake_evaluation(monkeypatch):
    monkeypatch.setattr(engine, "_evaluate_variant_worker", _fake_worker)
    # Keep the patch in-process whatever the platform's start method.
    monkeypatch.setattr(engine, "ProcessPoolExecutor", ThreadPoolExecutor)


def _scheduler(db):
    return build_scheduler_from_settings(
        START, END, NurseManager(db), WeekendHistory(db), PreScheduler(db), SETTINGS
    )


def test_run_generation_reports_stages_and_progress(tmp_path, fake_evaluation):
    stages, progress = [], []
    run = _scheduler(seed_db(tmp_path)).run_generation(
        max_workers=2, on_stage=stages.append, on_progress=lambda d, t: progress.append((d, t))
    )

    assert run.status == "ok"
    total = len(run.candidates)
    assert stages == [
        "Building weekend rotation variants…",
        f"Evaluating {total} variants…",
        "Ranking variants…",
    ]
    assert progress[0] == (0, total) and progress[-1] == (total, total)
    assert [c[1]["rank"] for c in run.candidates] == list(range(1, total + 1))


def test_run_generation_can_be_cancelled_while_evaluating(tmp_path, monkeypatch):
    release = threading.Event()

    def slow_worker(item):
        release.wait(5)
        return _fake_worker(item)

    monkeypatch.setattr(engine, "_evaluate_variant_worker", slow_worker)
    cancel = threading.Event()
    stages = []

    def on_stage(text):
        stages.append(text)
        if text.startswith("Evaluating"):
            cancel.set()  # the user presses Cancel as evaluation starts

    try:
        run = _scheduler(seed_db(tmp_path)).run_generation(
            max_workers=2, use_threads=True, on_stage=on_stage, is_cancelled=cancel.is_set
        )
    finally:
        release.set()

    assert run.status == "cancelled"
    assert run.candidates == []


def test_run_generation_reports_infeasibility(tmp_path, fake_evaluation):
    # Two nurses cannot staff two weekends a week apart with a 14-day gap.
    db = seed_db(tmp_path, roster=[("A", False, False), ("B", False, False)])
    assert _scheduler(db).run_generation().status == "infeasible"


def test_generate_schedule_leaves_pdf_export_to_the_caller(tmp_path, monkeypatch, fake_evaluation):
    monkeypatch.chdir(tmp_path)
    scheduler = _scheduler(seed_db(tmp_path))

    best = scheduler.generate_schedule(top_n=2, max_workers=1)

    assert len(best) == 2
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".pdf")]
    out = tmp_path / "pdfs"
    out.mkdir()
    paths = scheduler.export_top_variants_as_pdfs(best, 2, out)
    assert [os.path.basename(p) for p in paths] == [
        "schedule_variant_1.pdf",
        "schedule_variant_2.pdf",
    ]
    assert all(os.path.getsize(p) > 0 for p in paths)


def test_the_gui_and_the_cli_rank_identically(qapp, tmp_path, monkeypatch, fake_evaluation):
    pytest.importorskip("PySide6.QtWidgets")
    from ui.worker_threads import DB_NAME, ScheduleProgressWorker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NSCHED_FORCE_THREAD_POOL", "1")
    db = seed_db(tmp_path, filename=DB_NAME)

    worker = ScheduleProgressWorker(START, END, settings=SETTINGS)
    worker.run()
    gui = [(c[0], c[1]["rank"], c[1]["weighted_score"]) for c in worker.all_candidates]

    cli_scheduler = _scheduler(db)
    cli = [
        (c[0], c[1]["rank"], c[1]["weighted_score"])
        for c in cli_scheduler.generate_schedule(
            top_n=100, weekend_variant_mode=cli_scheduler.WeekendVariantMode.STRICT_ONLY
        )
    ]

    assert gui and gui == cli
