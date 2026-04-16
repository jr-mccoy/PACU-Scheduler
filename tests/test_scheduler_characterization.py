import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import pytest

from NCSSQL57 import (
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

    d1 = pd.Timestamp("2026-01-02")
    d2 = pd.Timestamp("2026-01-09")

    history.add_assignment(d1, "Alice", "Bob")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    history.add_assignment(d2, "Alice", "Bob")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    backup = history.backup()

    history.modify_assignment(d2, "Bob", "Alice")
    _assert_weekend_history_consistent(history, temp_weekend_db)

    history.remove_assignment(d1)
    _assert_weekend_history_consistent(history, temp_weekend_db)

    history.restore(backup)
    _assert_weekend_history_consistent(history, temp_weekend_db)


def _build_variant(nurses=("Alice", "Bob")) -> ScheduleVariant:
    idx = pd.date_range("2026-01-02", periods=5, freq="D")
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


def test_tracker_restore_global_best_survives_early_success_path():
    variant = _build_variant()
    tracker = BestStateTracker(variant)
    tracker.initialize()

    day = variant.state.schedule.index[1]
    variant.state.schedule.at[day, "main"] = "Alice"
    tracker.restore_global_best()

    assert pd.isna(variant.state.schedule.at[day, "main"])


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


def test_generation_mode_strict_then_relaxed(monkeypatch, scheduler_for_modes):
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
    assert out == ["relaxed"]
    assert calls == ["strict", "relaxed"]


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

    nurse_counts_a = {"Alice": {"total": 2}}
    nurse_counts_b = {"Bob": {"total": 2}}

    rot_viol_a = scheduler._rotation_violation_score(nurse_counts_a, history.get_violation_counts())
    rot_viol_b = scheduler._rotation_violation_score(nurse_counts_b, history.get_violation_counts())
    assert rot_viol_a > rot_viol_b

    overage = {"Alice": 3, "Bob": 0, "Cara": 0}
    long_a = scheduler._long_term_score({"Alice": {"total": 4}, "Bob": {"total": 1}, "Cara": {"total": 1}}, overage)
    long_b = scheduler._long_term_score({"Alice": {"total": 1}, "Bob": {"total": 3}, "Cara": {"total": 1}}, overage)
    assert long_a > long_b

    idx = pd.date_range("2026-01-02", periods=28, freq="D")
    sched_a = pd.DataFrame(index=idx, columns=["main", "backup"], data=None)
    sched_b = pd.DataFrame(index=idx, columns=["main", "backup"], data=None)
    fridays = [d for d in idx if d.weekday() == 4]

    for d in fridays:
        sched_a.at[d, "main"] = "Alice"
        sched_a.at[d, "backup"] = "Bob"

    for d in fridays:
        sched_b.at[d, "main"] = "Bob"
        sched_b.at[d, "backup"] = "Cara"

    gap_a = scheduler._weekend_gap_penalty(sched_a)
    gap_b = scheduler._weekend_gap_penalty(sched_b)
    assert gap_a != gap_b

    candidates = [
        (0, {"rotation_rep": 2, "gaps": 1, "balance_main": 1, "balance_backup": 1}, nurse_counts_a, sched_a),
        (1, {"rotation_rep": 0, "gaps": 0, "balance_main": 0, "balance_backup": 0}, nurse_counts_b, sched_b),
    ]
    scheduler._score_and_rank_variants(candidates)
    assert candidates[0][1]["weighted_score"] != candidates[1][1]["weighted_score"]
