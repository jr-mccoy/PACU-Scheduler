import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import pytest
from scheduler import debug as scheduler_module

from scheduler import (
    BestStateTracker,
    Comparison,
    NurseScheduler,
    ScheduleState,
    ScheduleVariant,
    SchedulerConfig,
    WeekendHistory,
    WeekendPattern,
)


class DummyNurseManager:
    def __init__(self, db_name: str = ":memory:"):
        self.db_name = db_name

    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


class DummyPreScheduler:
    def get_assignments_in_range(self, start_date, end_date):
        return {}


class StaticWeekendHistory:
    def __init__(self, weekends=None, last_patterns=None, violation_counts=None):
        self._weekends = weekends or {}
        self._last = last_patterns or {}
        self._viol = violation_counts or {}

    def get_last_weekend_before(self, nurse: str, before_date: pd.Timestamp):
        candidates = [w for w in self._weekends.get(nurse, []) if w < before_date]
        return max(candidates) if candidates else None

    def get_last_pattern(self, nurse: str):
        return self._last.get(nurse)

    def get_weekends(self, nurse: str):
        return list(self._weekends.get(nurse, []))

    def get_violation_counts(self):
        return dict(self._viol)


@pytest.fixture
def temp_weekend_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "weekend_history.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE nurses (
                nurse_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE weekend_assignments (
                weekend_start TEXT PRIMARY KEY,
                fsf_nurse_id INTEGER,
                sfs_nurse_id INTEGER,
                FOREIGN KEY(fsf_nurse_id) REFERENCES nurses(nurse_id),
                FOREIGN KEY(sfs_nurse_id) REFERENCES nurses(nurse_id)
            )
            """
        )
        conn.executemany(
            "INSERT INTO nurses(name) VALUES(?)",
            [("Alice",), ("Bob",), ("Cara",)],
        )
    return db_path


def _table_assignments(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT wa.weekend_start, nf.name, ns.name
            FROM weekend_assignments wa
            LEFT JOIN nurses nf ON wa.fsf_nurse_id = nf.nurse_id
            LEFT JOIN nurses ns ON wa.sfs_nurse_id = ns.nurse_id
            ORDER BY wa.weekend_start
            """
        ).fetchall()
    return [(pd.Timestamp(d).normalize(), fsf, sfs) for d, fsf, sfs in rows]


def _table_last_patterns(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.name, wrh.last_pattern
            FROM weekend_rotation_history wrh
            JOIN nurses n ON wrh.nurse_id = n.nurse_id
            """
        ).fetchall()
    parsed = {}
    for nurse, pattern in rows:
        parsed[nurse] = WeekendPattern(pattern) if pattern else None
    return parsed


def _table_violation_counts(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.name, COALESCE(vs.violation_count, 0)
            FROM nurses n
            LEFT JOIN rotation_violation_stats vs ON n.nurse_id = vs.nurse_id
            """
        ).fetchall()
    return dict(rows)


def _table_violation_dates(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.name, vd.violation_date, vd.pattern, vd.previous_pattern
            FROM rotation_violation_dates vd
            JOIN nurses n ON vd.nurse_id = n.nurse_id
            ORDER BY n.name, vd.violation_date
            """
        ).fetchall()
    return rows


def _assert_weekend_history_consistent(history: WeekendHistory, db_path: Path):
    assignments = history.get_assignments()
    table_assignments = _table_assignments(db_path)
    assert assignments == table_assignments

    table_patterns = _table_last_patterns(db_path)
    for nurse in ["Alice", "Bob", "Cara"]:
        assert history.get_last_pattern(nurse) == table_patterns.get(nurse)

    api_counts = history.get_violation_counts()
    table_counts = _table_violation_counts(db_path)
    assert api_counts == table_counts

    api_dates = history.get_violation_dates()
    table_dates = _table_violation_dates(db_path)
    assert api_dates == table_dates

    # Mutual consistency between date and count APIs.
    from_dates = {}
    for nurse, *_ in api_dates:
        from_dates[nurse] = from_dates.get(nurse, 0) + 1
    for nurse, count in api_counts.items():
        assert count == from_dates.get(nurse, 0)


# anchor: class WeekendHistory

def test_weekend_history_integrity_operations(temp_weekend_db: Path):
    history = WeekendHistory(str(temp_weekend_db))
    service = history.weekend_service

    d1 = pd.Timestamp("2026-01-02")
    d2 = pd.Timestamp("2026-01-09")

    service.add_assignment(d1, "Alice", "Bob")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    service.add_assignment(d2, "Alice", "Bob")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    backup = history.backup()

    service.modify_assignment(d2, "Bob", "Alice")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    service.remove_assignment(d1)
    _assert_weekend_history_consistent(history, temp_weekend_db)

    service.restore_assignments(backup)
    assert history.get_assignments() == backup
    assert history.get_last_pattern("Alice") == WeekendPattern.FSF
    assert history.get_last_pattern("Bob") == WeekendPattern.SFS
    assert history.get_last_pattern("Cara") is None
    assert history.get_violation_counts() == {"Alice": 1, "Bob": 1, "Cara": 0}
    assert history.get_violation_dates() == [
        ("Alice", "2026-01-09", "FSF", "FSF"),
        ("Bob", "2026-01-09", "SFS", "SFS"),
    ]
    _assert_weekend_history_consistent(history, temp_weekend_db)


def test_violation_manual_override_persists_until_explicit_rebuild(temp_weekend_db: Path):
    history = WeekendHistory(str(temp_weekend_db))
    weekend_service = history.weekend_service
    violation_service = history.violation_service

    weekend_service.add_assignment(pd.Timestamp("2026-01-02"), "Alice", "Bob")
    weekend_service.add_assignment(pd.Timestamp("2026-01-09"), "Alice", "Bob")
    assert history.get_violation_counts()["Alice"] == 1

    history.set_violation_count("Alice", 7)
    assert history.get_violation_counts()["Alice"] == 7

    violation_service.rebuild()
    assert history.get_violation_counts()["Alice"] == 1


def _build_variant(nurses=("Alice", "Bob"), periods=5) -> ScheduleVariant:
    idx = pd.date_range("2026-01-02", periods=periods, freq="D")
    schedule = pd.DataFrame(index=idx, columns=["main", "backup"], data=None)
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
    return ScheduleVariant(
        state=state,
        nurses=list(nurses),
        availability=schedule.copy(),
        config=SchedulerConfig(),
        nurse_manager=DummyNurseManager(),
        pre_scheduled={},
        console_debug=False,
    )


def _build_scheduler(start="2026-01-02", end="2026-01-04", nurses=("Alice", "Bob")) -> NurseScheduler:
    return NurseScheduler(
        start_date=pd.Timestamp(start),
        end_date=pd.Timestamp(end),
        nurses=list(nurses),
        prn_nurses=[],
        nurse_manager=DummyNurseManager(),
        weekend_history=StaticWeekendHistory(),
        pre_scheduler=DummyPreScheduler(),
        config=SchedulerConfig(),
    )


# anchor: class BestStateTracker

def test_tracker_begin_iteration_and_revert_restores_snapshot():
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    tracker.initialize()

    tracker.begin_iteration("revert-check")
    variant.state.main_assignment_counts["Alice"] = 7
    variant.state.rotation_repeats = 3

    result = tracker.evaluate_and_commit(phase_name="revert-check")
    assert result == Comparison.WORSE
    assert variant.state.main_assignment_counts["Alice"] == 0
    assert variant.state.rotation_repeats == 0


def test_tracker_global_best_updates_only_on_strict_improvement():
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    baseline = tracker.initialize()

    tracker.begin_iteration("improve")
    variant.state.rotation_repeats = 2
    worse = tracker.evaluate_and_commit(phase_name="improve")
    assert worse == Comparison.WORSE
    assert tracker.get_global_best_quality() == baseline

    tracker.begin_iteration("neutral")
    neutral = tracker.evaluate_and_commit(phase_name="neutral", allow_neutral=True)
    assert neutral == Comparison.EQUAL
    assert tracker.get_global_best_quality() == baseline


def test_get_valid_nurse_pairs_rejects_prefilled_weekend_conflict():
    scheduler = _build_scheduler()
    weekend = pd.Timestamp("2026-01-02")

    # Partially prefilled weekend: only Saturday main is set.
    # For pair (Alice, Bob), Sat main should be Bob -> this must be rejected.
    scheduler.schedule.at[pd.Timestamp("2026-01-03"), "main"] = "Alice"

    pairs = scheduler._get_valid_nurse_pairs(
        weekend=weekend,
        last_assignment=scheduler.last_assignment,
        last_pattern=scheduler.last_pattern,
        pre_scheduled={},
        weekend_tracking=scheduler.weekend_tracking,
        schedule=scheduler.schedule,
        all_pre_scheduled_weekends={},
        enforce_rotation=True,
    )

    assert ("Alice", "Bob") not in pairs
    assert ("Bob", "Alice") in pairs


def test_tracker_restore_global_best_survives_early_success_path():
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    tracker.initialize()

    day = variant.state.schedule.index[1]
    variant.state.schedule.at[day, "main"] = "Alice"
    tracker.restore_global_best()

    assert pd.isna(variant.state.schedule.at[day, "main"])


def test_window_refill_target_hit_commits_before_early_return(monkeypatch):
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    tracker.initialize()

    days = list(variant.state.schedule.index[:2])
    evaluate_calls = {"count": 0}
    spread_values = iter(
        [
            (5, 5, 0),  # initial good_enough() check
            (5, 5, 0),  # base_tuple for first window
            (1, 1, 0),  # new_tuple for first window
            (1, 1, 0),  # good_enough() after accepted change
        ]
    )

    monkeypatch.setattr(variant, "_collect_weekday_windows", lambda window_weeks: [days])
    monkeypatch.setattr(variant, "backup_week_assignments", lambda d: {"days": list(d)})
    monkeypatch.setattr(variant, "_clear_window_assignments", lambda d: None)
    monkeypatch.setattr(variant, "_build_window_varlist", lambda d: [])
    monkeypatch.setattr(variant, "_backtrack_window", lambda *args, **kwargs: True)
    monkeypatch.setattr(variant, "_restore_from_backup", lambda d, backup: None)
    monkeypatch.setattr(variant, "_spread_components", lambda: next(spread_values))

    original_evaluate = tracker.evaluate_and_commit

    def _counting_evaluate(*args, **kwargs):
        evaluate_calls["count"] += 1
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(tracker, "evaluate_and_commit", _counting_evaluate)

    result = variant.iterative_window_refill_rebalance(
        window_weeks=1,
        max_passes=1,
        time_limit_ms=1,
        node_limit=1,
        target_spread=(1, 1),
        tracker=tracker,
    )

    assert result is True
    assert evaluate_calls["count"] == 1
    assert tracker._global_best is not None
    pd.testing.assert_frame_equal(variant.state.schedule, tracker._global_best.schedule)


@pytest.fixture
def scheduler_for_modes():
    nurses = ["Alice", "Bob"]
    wh = StaticWeekendHistory()
    return NurseScheduler(
        start_date="2026-01-01",
        end_date="2026-01-31",
        nurses=nurses,
        prn_nurses=[],
        nurse_manager=DummyNurseManager(),
        weekend_history=wh,
        pre_scheduler=DummyPreScheduler(),
        config=SchedulerConfig(),
    )


# anchor: def generate_schedule

def test_generation_mode_strict_only(monkeypatch, scheduler_for_modes):
    calls = []

    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_strict_variants",
        lambda variants, friday, fixed, pre: calls.append("strict") or ["strict"],
    )
    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_relaxed_variants",
        lambda *args, **kwargs: calls.append("relaxed") or ["relaxed"],
    )

    out = scheduler_for_modes._process_weekend_variants(
        pd.Timestamp("2026-01-02"), [_build_variant()], {}, allow_rotation_violations=False
    )
    assert out == ["strict"]
    assert calls == ["strict"]


def test_generation_mode_strict_never_relaxes_per_weekend(monkeypatch, scheduler_for_modes):
    """A strict pass that yields nothing must NOT silently fall back to the
    relaxed branch inside the same call — that fallback made STRICT_ONLY
    non-strict and left the STRICT_THEN_RELAXED confirmation gate dead."""
    calls = []
    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_strict_variants",
        lambda *args, **kwargs: calls.append("strict") or [],
    )
    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_relaxed_variants",
        lambda *args, **kwargs: calls.append("relaxed") or ["relaxed"],
    )

    out = scheduler_for_modes._process_weekend_variants(
        pd.Timestamp("2026-01-02"), [_build_variant()], {}, allow_rotation_violations=False
    )
    assert out == []
    assert calls == ["strict"]


def test_strict_then_relaxed_gate_declined_aborts(monkeypatch, scheduler_for_modes):
    calls = []

    def fake_generate(*, allow_rotation_violations=False):
        calls.append(allow_rotation_violations)
        return []

    monkeypatch.setattr(
        scheduler_for_modes, "generate_all_weekend_variants", fake_generate
    )

    out = scheduler_for_modes._generate_weekend_variants(
        confirm_rotation_callback=lambda: False,
        weekend_variant_mode=NurseScheduler.WeekendVariantMode.STRICT_THEN_RELAXED,
    )
    assert out == []
    # Strict attempt only; the relaxed retry must not run when declined.
    assert calls == [False]


def test_strict_then_relaxed_gate_accepted_retries_relaxed(monkeypatch, scheduler_for_modes):
    calls = []
    sentinel = ["relaxed-variant"]

    def fake_generate(*, allow_rotation_violations=False):
        calls.append(allow_rotation_violations)
        return sentinel if allow_rotation_violations else []

    monkeypatch.setattr(
        scheduler_for_modes, "generate_all_weekend_variants", fake_generate
    )

    out = scheduler_for_modes._generate_weekend_variants(
        confirm_rotation_callback=lambda: True,
        weekend_variant_mode=NurseScheduler.WeekendVariantMode.STRICT_THEN_RELAXED,
    )
    assert out == sentinel
    assert calls == [False, True]


def test_generation_mode_relaxed_immediately(monkeypatch, scheduler_for_modes):
    calls = []
    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_strict_variants",
        lambda *args, **kwargs: calls.append("strict") or ["strict"],
    )
    monkeypatch.setattr(
        scheduler_for_modes,
        "_generate_relaxed_variants",
        lambda *args, **kwargs: calls.append("relaxed") or ["relaxed"],
    )

    out = scheduler_for_modes._process_weekend_variants(
        pd.Timestamp("2026-01-02"), [_build_variant()], {}, allow_rotation_violations=True
    )
    assert out == ["relaxed"]
    assert calls == ["relaxed"]


# anchor: def assign_weekend

def test_assign_weekend_records_rotation_repeat_on_variant():
    friday = pd.Timestamp("2026-01-02")
    variant = _build_variant()
    variant.state.last_pattern["Alice"] = WeekendPattern.FSF

    variant.assign_weekend(friday, "Alice", "Bob")  # Alice repeats FSF

    assert variant.rotation_violations == [
        (friday, "Alice", WeekendPattern.FSF.value)
    ]
    assert variant.state.rotation_repeats == 1


def test_assign_weekend_no_repeat_records_nothing():
    variant = _build_variant()
    variant.state.last_pattern["Alice"] = WeekendPattern.SFS

    variant.assign_weekend(pd.Timestamp("2026-01-02"), "Alice", "Bob")

    assert variant.rotation_violations == []
    assert variant.state.rotation_repeats == 0


def test_clone_isolates_rotation_violations():
    friday = pd.Timestamp("2026-01-02")
    parent = _build_variant()
    parent.state.last_pattern["Alice"] = WeekendPattern.FSF

    child = parent.clone()
    child.assign_weekend(friday, "Alice", "Bob")

    assert parent.rotation_violations == []
    assert child.rotation_violations == [
        (friday, "Alice", WeekendPattern.FSF.value)
    ]


def test_collect_rotation_violations_deduplicates_shared_ancestry(scheduler_for_modes):
    friday = pd.Timestamp("2026-01-02")
    parent = _build_variant(periods=10)
    parent.state.last_pattern["Alice"] = WeekendPattern.FSF

    # Two surviving branches that share the same inherited violation, one of
    # which adds branch-specific violations later.
    branch_a = parent.clone()
    branch_a.assign_weekend(friday, "Alice", "Bob")
    branch_b = branch_a.clone()
    next_friday = pd.Timestamp("2026-01-09")
    branch_b.assign_weekend(next_friday, "Alice", "Bob")

    scheduler_for_modes._collect_rotation_violations([branch_a, branch_b])

    history = scheduler_for_modes.get_rotation_violation_history()
    # Shared violation counted once despite appearing in both branches.
    assert history["Alice"].count(friday) == 1
    assert (next_friday.isoformat(), "Bob", WeekendPattern.SFS.value) in (
        scheduler_for_modes._rotation_violations
    )


# anchor: _rotation_violation_score

def test_metrics_diverge_and_weighted_score_reflects_difference():
    history = StaticWeekendHistory(
        weekends={"Alice": [pd.Timestamp("2025-12-26")], "Bob": [], "Cara": []},
        violation_counts={"Alice": 4, "Bob": 0, "Cara": 0},
    )
    scheduler = NurseScheduler(
        start_date="2026-01-01",
        end_date="2026-01-31",
        nurses=["Alice", "Bob", "Cara"],
        prn_nurses=[],
        nurse_manager=DummyNurseManager(),
        weekend_history=history,
        pre_scheduler=DummyPreScheduler(),
        config=SchedulerConfig(),
    )

    idx = pd.date_range("2026-01-02", periods=28, freq="D")
    sched_a = pd.DataFrame(index=idx, columns=["main", "backup"], data=None)
    sched_b = pd.DataFrame(index=idx, columns=["main", "backup"], data=None)

    # Candidate A uses Alice on Fri/Sat/Sun every weekend.
    for d in idx:
        if d.weekday() in (4, 5, 6):
            sched_a.at[d, "main"] = "Alice"
            sched_a.at[d, "backup"] = "Bob"

    # Candidate B avoids Alice on weekend rows.
    for d in idx:
        if d.weekday() in (4, 5, 6):
            sched_b.at[d, "main"] = "Bob"
            sched_b.at[d, "backup"] = "Cara"

    nurse_counts_a = {"Alice": {"total": 2}, "Bob": {"total": 2}}
    nurse_counts_b = {"Bob": {"total": 2}, "Cara": {"total": 2}}
    rot_viol_a = scheduler._rotation_violation_score(
        nurse_counts_a, history.get_violation_counts(), sched_a
    )
    rot_viol_b = scheduler._rotation_violation_score(
        nurse_counts_b, history.get_violation_counts(), sched_b
    )
    assert rot_viol_a > rot_viol_b

    overage = {"Alice": 3, "Bob": 0, "Cara": 0}
    long_a = scheduler._long_term_score({"Alice": {"total": 4}, "Bob": {"total": 1}, "Cara": {"total": 1}}, overage)
    long_b = scheduler._long_term_score({"Alice": {"total": 1}, "Bob": {"total": 3}, "Cara": {"total": 1}}, overage)
    assert long_a > long_b

    gap_a = scheduler._weekend_gap_penalty(sched_a)
    gap_b = scheduler._weekend_gap_penalty(sched_b)
    assert gap_a != gap_b

    candidates = [
        (0, {"rotation_rep": 2, "gaps": 1, "balance_main": 1, "balance_backup": 1}, nurse_counts_a, sched_a),
        (1, {"rotation_rep": 0, "gaps": 0, "balance_main": 0, "balance_backup": 0}, nurse_counts_b, sched_b),
    ]
    scheduler._score_and_rank_variants(candidates)
    assert candidates[0][1]["weighted_score"] != candidates[1][1]["weighted_score"]


def test_draw_week_rows_blank_cells_do_not_render_literal_none():
    scheduler = NurseScheduler(
        start_date="2026-01-01",
        end_date="2026-01-31",
        nurses=["Alice", "Bob"],
        prn_nurses=[],
        nurse_manager=DummyNurseManager(),
        weekend_history=StaticWeekendHistory(),
        pre_scheduler=DummyPreScheduler(),
        config=SchedulerConfig(),
    )

    class FakeCanvas:
        def __init__(self):
            self.centered_text = []

        def rect(self, *args, **kwargs):
            return None

        def setFont(self, *args, **kwargs):
            return None

        def drawString(self, *args, **kwargs):
            return None

        def drawCentredString(self, _x, _y, text):
            self.centered_text.append(text)

    dt_blank = pd.Timestamp("2026-01-03")
    dt_filled = pd.Timestamp("2026-01-04")
    sched_df = pd.DataFrame(
        index=[dt_blank, dt_filled],
        data={"main": [None, "Alice"], "backup": [None, "Bob"]},
    )

    canvas = FakeCanvas()
    scheduler._draw_week_rows(
        canvas,
        weeks=[[0, 0, 0, 0, 0, 0, 3], [4, 0, 0, 0, 0, 0, 0]],
        year=2026,
        month=1,
        sched_df=sched_df,
        y_top=100,
        row_h=20,
        col_w=20,
    )

    assert "None" not in canvas.centered_text
    assert "" in canvas.centered_text
    assert "Alice" in canvas.centered_text
    assert "Bob" in canvas.centered_text


def test_module_log_reuses_cached_file_per_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scheduler_module, "_DEBUG", True)
    scheduler_module._LOG_FILE_CACHE.clear()

    scheduler_module.log("assignments", {"event": 1})
    scheduler_module.log("assignments", {"event": 2})

    log_files = list(tmp_path.glob("assignments_dump_*.log"))
    assert len(log_files) == 1
    assert scheduler_module._LOG_FILE_CACHE["assignments"] == log_files[0].name


def test_window_refill_target_hit_short_circuits_before_search(monkeypatch: pytest.MonkeyPatch):
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    tracker.initialize()

    monkeypatch.setattr(variant, "_collect_weekday_windows", lambda window_weeks=3: [[variant.state.schedule.index[0]]])

    def fail_backtrack(*_args, **_kwargs):
        raise AssertionError("backtrack should not run when target already met")

    monkeypatch.setattr(variant.window_optimizer, "backtrack_window", fail_backtrack)
    assert variant.iterative_window_refill_rebalance(target_spread=(1, 1), tracker=tracker) is True


def test_window_refill_restores_global_best_after_mutation(monkeypatch: pytest.MonkeyPatch):
    variant = _build_variant()
    first_day = variant.state.schedule.index[0]
    second_day = variant.state.schedule.index[1]
    variant.state.schedule.at[first_day, "main"] = "Alice"
    variant._recalculate_assignment_counts()
    variant._update_last_assignment_dates()

    baseline = variant.state.schedule.copy(deep=True)

    monkeypatch.setattr(variant, "_collect_weekday_windows", lambda window_weeks=3: [[second_day]])

    def mutate_backtrack(vars_list, *_args, **_kwargs):
        day, role = vars_list[0]
        variant.state.schedule.at[day, role] = "Bob"
        return True

    monkeypatch.setattr(variant.window_optimizer, "backtrack_window", mutate_backtrack)
    monkeypatch.setattr(variant, "_lexi_better", lambda _new, _base: False)

    variant.iterative_window_refill_rebalance(max_passes=1, target_spread=(0, 0))
    pd.testing.assert_frame_equal(variant.state.schedule, baseline)


def test_domain_builder_boundary_preserves_strict_vs_relaxed_modes(monkeypatch: pytest.MonkeyPatch):
    variant = _build_variant()
    calls = []

    def capture(day, role, diagnostics=None, *, force_relaxed=False, gap_mode=False):
        calls.append((day, role, force_relaxed, gap_mode))
        return []

    monkeypatch.setattr(variant.domain_builder, "eligible_domain", capture)
    day = variant.state.schedule.index[0]

    variant._eligible_domain(day, "main", force_relaxed=False)
    variant._eligible_domain_gap(day, "backup", force_relaxed=True)

    assert calls[0] == (day, "main", False, False)
    assert calls[1] == (day, "backup", True, True)
