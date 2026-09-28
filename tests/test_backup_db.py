"""scripts/backup_db.py: a consistent dated copy, keeping only the newest few."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scheduler import NurseManager

_SPEC = importlib.util.spec_from_file_location(
    "backup_db", Path(__file__).resolve().parents[1] / "scripts" / "backup_db.py"
)
backup_db = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup_db)


def test_backup_copies_the_database_and_prunes_old_copies(tmp_path):
    db = tmp_path / "nurse_schedule.db"
    NurseManager(str(db)).add_nurse("A")
    dest = tmp_path / "backups"

    for day in (1, 2, 3):
        backup_db.backup(db, dest, keep=2, today=date(2026, 9, day))

    copies = sorted(p.name for p in dest.iterdir())
    assert copies == ["nurse_schedule-2026-09-02.db", "nurse_schedule-2026-09-03.db"]
    with sqlite3.connect(dest / copies[-1]) as conn:
        assert conn.execute("SELECT name FROM nurses").fetchall() == [("A",)]


def test_backup_of_a_missing_database_fails_without_creating_one(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        backup_db.backup(missing, tmp_path / "backups", keep=3)
    assert not missing.exists()
    assert backup_db.main(["--db", str(missing), "--dest", str(tmp_path / "b")]) == 1
