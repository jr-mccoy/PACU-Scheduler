"""Regression tests for the WeekendHistory canonical/derived rebuild contract.

These cover the bug classes documented in ``code-review.md`` lines 183-298:

* editing an old weekend does not overwrite the real latest pattern
* removing a weekend restores the correct prior pattern
* restore() round-trip rebuilds both assignments and derived state

The tests intentionally avoid importing the ``NCSSQL57`` compatibility shim so
they can run in isolation from the GUI surface.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler import WeekendHistory, WeekendPattern  # noqa: E402


@pytest.fixture
def weekend_db(tmp_path: Path) -> Path:
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
            [("Alice",), ("Bob",), ("Cara",), ("Dan",)],
        )
    return db_path


def _last_patterns_in_db(db_path: Path) -> dict[str, WeekendPattern]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.name, wrh.last_pattern
            FROM weekend_rotation_history wrh
            JOIN nurses n ON wrh.nurse_id = n.nurse_id
            """
        ).fetchall()
    return {name: WeekendPattern(pat) for name, pat in rows if pat}


def _violation_counts_in_db(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.name, COALESCE(vs.violation_count, 0)
            FROM nurses n
            LEFT JOIN rotation_violation_stats vs
                ON vs.nurse_id = n.nurse_id
            """
        ).fetchall()
    return {name: count for name, count in rows}


def test_editing_old_weekend_does_not_overwrite_latest_pattern(weekend_db: Path):
    """Modifying a historical weekend must not pin an out-of-date pattern."""
    history = WeekendHistory(str(weekend_db))

    history.add_assignment(pd.Timestamp("2026-01-02"), "Alice", "Bob")
    history.add_assignment(pd.Timestamp("2026-01-09"), "Bob", "Alice")
    history.add_assignment(pd.Timestamp("2026-01-16"), "Alice", "Bob")

    assert history.get_last_pattern("Alice") == WeekendPattern.FSF
    assert history.get_last_pattern("Bob") == WeekendPattern.SFS

    # Edit the OLDEST weekend. Naive incremental updates would clobber the
    # latest pattern with the edited weekend's pattern.
    history.modify_assignment(pd.Timestamp("2026-01-02"), "Cara", "Dan")

    # The newest weekend (2026-01-16) still pins Alice=FSF and Bob=SFS.
    assert history.get_last_pattern("Alice") == WeekendPattern.FSF
    assert history.get_last_pattern("Bob") == WeekendPattern.SFS
    # Cara/Dan get their first-ever pattern (from the now-edited oldest week).
    assert history.get_last_pattern("Cara") == WeekendPattern.FSF
    assert history.get_last_pattern("Dan") == WeekendPattern.SFS

    # Database and in-memory caches agree.
    db_patterns = _last_patterns_in_db(weekend_db)
    for nurse in ["Alice", "Bob", "Cara", "Dan"]:
        assert history.get_last_pattern(nurse) == db_patterns.get(nurse)


def test_removing_weekend_restores_prior_pattern(weekend_db: Path):
    """Removing the latest weekend must roll the last-pattern back to the prior assignment."""
    history = WeekendHistory(str(weekend_db))

    history.add_assignment(pd.Timestamp("2026-01-02"), "Alice", "Bob")
    history.add_assignment(pd.Timestamp("2026-01-09"), "Bob", "Alice")
    assert history.get_last_pattern("Alice") == WeekendPattern.SFS
    assert history.get_last_pattern("Bob") == WeekendPattern.FSF

    history.remove_assignment(pd.Timestamp("2026-01-09"))

    # The remaining weekend (2026-01-02) is now the latest, pinning Alice=FSF,
    # Bob=SFS.
    assert history.get_last_pattern("Alice") == WeekendPattern.FSF
    assert history.get_last_pattern("Bob") == WeekendPattern.SFS

    history.remove_assignment(pd.Timestamp("2026-01-02"))

    # No assignments left → no last-pattern rows.
    assert history.get_last_pattern("Alice") is None
    assert history.get_last_pattern("Bob") is None
    assert _last_patterns_in_db(weekend_db) == {}


def test_restore_round_trip_rebuilds_assignments_and_derived(weekend_db: Path):
    """``restore()`` must reinstate both canonical assignments and derived state."""
    history = WeekendHistory(str(weekend_db))

    history.add_assignment(pd.Timestamp("2026-01-02"), "Alice", "Bob")
    history.add_assignment(pd.Timestamp("2026-01-09"), "Alice", "Bob")  # creates violations
    history.add_assignment(pd.Timestamp("2026-01-16"), "Cara", "Dan")

    backup = history.backup()
    baseline_patterns = {
        nurse: history.get_last_pattern(nurse)
        for nurse in ["Alice", "Bob", "Cara", "Dan"]
    }
    baseline_violations = history.get_violation_counts()

    # Mutate everything.
    history.remove_assignment(pd.Timestamp("2026-01-02"))
    history.modify_assignment(pd.Timestamp("2026-01-16"), "Dan", "Cara")
    history.add_assignment(pd.Timestamp("2026-01-23"), "Bob", "Alice")

    assert history.get_assignments() != backup

    history.restore(backup)

    assert history.get_assignments() == backup
    for nurse in ["Alice", "Bob", "Cara", "Dan"]:
        assert history.get_last_pattern(nurse) == baseline_patterns[nurse]
    assert history.get_violation_counts() == baseline_violations

    # Database mirrors the in-memory state.
    db_patterns = _last_patterns_in_db(weekend_db)
    for nurse, pat in baseline_patterns.items():
        if pat is None:
            assert nurse not in db_patterns
        else:
            assert db_patterns[nurse] == pat
    assert _violation_counts_in_db(weekend_db) == baseline_violations


def test_manual_overrides_persist_until_explicit_rebuild(weekend_db: Path):
    """Manual ``set_*`` overrides hold until a canonical-write rebuild fires."""
    history = WeekendHistory(str(weekend_db))

    history.add_assignment(pd.Timestamp("2026-01-02"), "Alice", "Bob")
    history.add_assignment(pd.Timestamp("2026-01-09"), "Alice", "Bob")
    assert history.get_violation_counts()["Alice"] == 1

    history.set_violation_count("Alice", 7)
    assert history.get_violation_counts()["Alice"] == 7

    # Manual last-pattern override: pin Bob to FSF even though canonical SFS.
    history.set_last_pattern("Bob", WeekendPattern.FSF)
    assert history.get_last_pattern("Bob") == WeekendPattern.FSF

    # Adding a new canonical assignment triggers a full rebuild and the
    # manual overrides are reconciled against the canonical store.
    history.add_assignment(pd.Timestamp("2026-01-16"), "Cara", "Dan")

    assert history.get_violation_counts()["Alice"] == 1
    assert history.get_last_pattern("Bob") == WeekendPattern.SFS


def test_derived_state_writers_consolidated_on_services(weekend_db: Path):
    """Only the service rebuild path may write derived weekend tables.

    Guards against re-introducing the dead-code helpers
    (``_recompute_last_pattern``, ``_add_violation_date``, ``_clear_violation_dates``,
    ``_set_violation_count``) that previously bypassed the canonical store.
    """
    history = WeekendHistory(str(weekend_db))
    forbidden = (
        "_recompute_last_pattern",
        "_add_violation_date",
        "_clear_violation_dates",
        "_set_violation_count",
        "_record_violation_stat",
    )
    for name in forbidden:
        assert not hasattr(history, name), (
            f"WeekendHistory.{name} should be gone (Phase 4 derived-state consolidation)"
        )
