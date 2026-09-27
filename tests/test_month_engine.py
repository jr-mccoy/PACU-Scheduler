"""The whole-month model as the app's engine, with the variant pipeline as fallback.

``run_generation`` (shared by the GUI, the CLI and ``generate_schedule``)
solves with ``scheduler.optimization.month_model`` by default, and falls
back to the weekend-variant pipeline whenever the model cannot be used.
"""

from __future__ import annotations

import threading
import time

import pytest
from scheduling_fixtures import build_scheduler, seed_db

import scheduler.optimization.month_model as month_model
from scheduler import SchedulerConfig, SharedSettings
from scheduler.factory import build_scheduler_config_from_settings

ROSTER6 = tuple((name, False, False) for name in "ABCDEF")
WEEKEND_1, WEEKEND_2 = ("2026-11-06", "2026-11-07", "2026-11-08"), ("2026-11-13", "2026-11-14")
SPLIT = [(n, d) for n in "DEF" for d in WEEKEND_1] + [(n, d) for n in "ABC" for d in WEEKEND_2]


def _small(tmp_path, **config):
    """Six nurses, two weekends: a month solves in a second or two."""
    db = seed_db(tmp_path, roster=ROSTER6, time_off=SPLIT)
    config.setdefault("month_options", 3)
    return build_scheduler(db, "2026-11-02", "2026-11-15", **config)


def _no_alternation(tmp_path, **config):
    """Strict FSF/SFS alternation has no solution (see test_month_model)."""
    history = [("2026-09-25", "A", "D"), ("2026-10-02", "B", "E"), ("2026-10-09", "C", "F")]
    db = seed_db(tmp_path, roster=ROSTER6, weekends=history, time_off=SPLIT)
    config.setdefault("month_options", 2)
    return build_scheduler(db, "2026-11-02", "2026-11-15", **config)


# ── the default path ───────────────────────────────────────────────────────
def test_run_generation_solves_the_whole_month_by_default(tmp_path):
    stages, progress = [], []
    run = _small(tmp_path).run_generation(
        on_stage=stages.append, on_progress=lambda d, t: progress.append((d, t))
    )

    assert run.status == "ok" and run.engine == "month"
    assert run.search_capped is False
    assert 1 <= len(run.candidates) <= 3
    assert stages[0] == "Solving the whole month…"
    assert progress[0] == (0, 3) and progress[-1][0] == len(run.candidates)
    assert [c[1]["rank"] for c in run.candidates] == list(range(1, len(run.candidates) + 1))
    for _idx, stats, counts, schedule in run.candidates:
        assert stats["solver"] == "month" and stats["solver_optimal"]
        for key in ("gaps", "balance_main", "balance_backup", "rotation_rep", "weighted_score"):
            assert key in stats
        assert set(counts) == set(run.candidates[0][2])
        assert not schedule[["main", "backup"]].loc[list(WEEKEND_1)].isna().any().any()
    fridays = [WEEKEND_1[0], WEEKEND_2[0]]
    weekends = {
        tuple(map(tuple, schedule.loc[fridays, ["main", "backup"]].to_numpy()))
        for *_rest, schedule in run.candidates
    }
    assert len(weekends) == len(run.candidates)  # different weekends each


def test_generate_schedule_returns_months(tmp_path):
    best = _small(tmp_path).generate_schedule(top_n=2)
    assert len(best) == 2 and all(c[1]["solver"] == "month" for c in best)


def test_the_month_beats_or_equals_the_variant_pipeline(tmp_path):
    month = _small(tmp_path).run_generation(on_progress=lambda d, t: None).candidates[0][1]
    variants = _small(tmp_path, engine="variants").run_generation(on_progress=lambda d, t: None)
    best = variants.candidates[0][1]

    def key(stats):
        return (stats["rotation_rep"], stats["gaps"])

    assert key(month) <= key(best)


# ── falling back to the variant pipeline ───────────────────────────────────
def test_unsupported_rules_fall_back_to_variants(tmp_path):
    roster = (*ROSTER6, ("P", True, False))
    db = seed_db(tmp_path, roster=roster, time_off=SPLIT, pre_scheduled=[("2026-11-06", "P", None)])
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15")
    stages = []
    run = scheduler.run_generation(on_stage=stages.append, on_progress=lambda d, t: None)

    assert run.engine == "variants"
    assert "Solving the whole month…" not in stages


@pytest.mark.parametrize(
    "failure",
    ["crash", "no month in time", "fails the checks"],
)
def test_a_failing_model_falls_back_to_variants(tmp_path, monkeypatch, failure):
    if failure == "crash":

        def solve(*args, **kwargs):
            raise RuntimeError("simulated model failure")

        monkeypatch.setattr(month_model, "solve_month_status", solve)
    elif failure == "no month in time":
        monkeypatch.setattr(month_model, "solve_month_status", lambda *a, **k: ("unknown", []))
    else:

        def replay(*args, **kwargs):
            raise ValueError("simulated rule break")

        monkeypatch.setattr(month_model, "replay", replay)

    stages = []
    run = _small(tmp_path).run_generation(on_stage=stages.append, on_progress=lambda d, t: None)

    assert run.status == "ok" and run.engine == "variants" and run.candidates
    assert "Using weekend variants instead…" in stages


def test_profiling_uses_the_variant_pipeline(tmp_path):
    run = _small(tmp_path).run_generation(profile=True, on_progress=lambda d, t: None)
    assert run.engine == "variants"


def test_the_variant_engine_can_be_chosen(tmp_path):
    run = _small(tmp_path, engine="variants").run_generation(on_progress=lambda d, t: None)
    assert run.engine == "variants" and run.candidates


# ── rotation modes ─────────────────────────────────────────────────────────
def test_strict_only_reports_infeasible(tmp_path):
    run = _no_alternation(tmp_path).run_generation(weekend_variant_mode="strict_only")
    assert run.status == "infeasible" and run.engine == "month"


def test_strict_then_relaxed_asks_and_respects_no(tmp_path):
    asked = []
    run = _no_alternation(tmp_path).run_generation(
        confirm_rotation_callback=lambda: asked.append(1) or False
    )
    assert asked == [1]
    assert run.status == "infeasible"


def test_strict_then_relaxed_asks_and_repeats_as_little_as_possible(tmp_path):
    asked, stages = [], []
    run = _no_alternation(tmp_path).run_generation(
        confirm_rotation_callback=lambda: asked.append(1) or True,
        on_stage=stages.append,
        on_progress=lambda d, t: None,
    )
    assert asked == [1]
    assert "Solving the whole month (rotation repeats allowed)…" in stages
    assert run.status == "ok" and run.engine == "month"
    assert min(c[1]["rotation_rep"] for c in run.candidates) == 2


def test_relaxed_allowed_goes_straight_to_repeats(tmp_path):
    asked = []
    run = _no_alternation(tmp_path).run_generation(
        weekend_variant_mode="relaxed_allowed",
        confirm_rotation_callback=lambda: asked.append(1) or True,
        on_progress=lambda d, t: None,
    )
    assert asked == []
    assert run.status == "ok" and run.candidates[0][1]["rotation_rep"] == 2


def test_a_month_with_nothing_to_relax_needs_no_question(tmp_path):
    asked = []
    run = _small(tmp_path).run_generation(
        confirm_rotation_callback=lambda: asked.append(1) or True, on_progress=lambda d, t: None
    )
    assert asked == [] and run.candidates[0][1]["rotation_rep"] == 0


# ── cancelling ─────────────────────────────────────────────────────────────
def test_a_run_can_be_cancelled_while_solving(tmp_path):
    db = seed_db(tmp_path)  # eight nurses, four weekends: seconds to solve
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", month_options=5)
    cancel = threading.Event()

    def on_stage(text):
        if text.startswith("Solving"):
            threading.Timer(0.5, cancel.set).start()

    began = time.perf_counter()
    run = scheduler.run_generation(on_stage=on_stage, is_cancelled=cancel.is_set)
    assert run.status == "cancelled" and run.engine == "month"
    assert time.perf_counter() - began < 10


# ── settings ───────────────────────────────────────────────────────────────
def test_the_month_engine_is_the_default_everywhere():
    assert SharedSettings.DEFAULTS["scheduling_engine"] == "month"
    assert SchedulerConfig().engine == "month"
    assert build_scheduler_config_from_settings(SharedSettings.DEFAULTS).engine == "month"


def test_settings_choose_the_engine():
    chosen = dict(SharedSettings.DEFAULTS, scheduling_engine="variants")
    assert build_scheduler_config_from_settings(chosen).engine == "variants"
    unknown = dict(SharedSettings.DEFAULTS, scheduling_engine="bogus")
    assert build_scheduler_config_from_settings(unknown).engine == "month"
    with pytest.raises(ValueError):
        SchedulerConfig(engine="bogus")


# ── the GUI worker ─────────────────────────────────────────────────────────
def test_the_gui_worker_offers_months(qapp, tmp_path, monkeypatch):
    from ui.worker_threads import DB_NAME, ScheduleProgressWorker

    monkeypatch.chdir(tmp_path)
    seed_db(tmp_path, filename=DB_NAME, roster=ROSTER6, time_off=SPLIT)
    settings = dict(SharedSettings.DEFAULTS, weekend_gap_days=14)
    worker = ScheduleProgressWorker("2026-11-02", "2026-11-15", settings=settings)
    errors, finished = [], []
    worker.error.connect(errors.append)
    worker.finished.connect(lambda *args: finished.append(args))
    worker.run()

    assert errors == []
    (options, _scheduler, _history) = finished[0]
    assert options and all(stats["solver"] == "month" for _i, stats, _c, _s in options)
    scores = [stats["weighted_score"] for _, stats, _, _ in options]
    assert scores == sorted(scores)
