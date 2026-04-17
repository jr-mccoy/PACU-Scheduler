"""Transactional mutation services for weekend/violation history state."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Iterable, Optional

import pandas as pd

if TYPE_CHECKING:
    from .legacy_core import WeekendHistory

logger = logging.getLogger(__name__)


class WeekendHistoryService:
    """Command-style writes for canonical weekend history plus derived rebuilds."""

    def __init__(self, history: "WeekendHistory"):
        self._history = history

    def add_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str) -> None:
        normalized = self._history._normalize_date(weekend_start)
        date_str = normalized.strftime("%Y-%m-%d")

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO weekend_assignments
                      (weekend_start, fsf_nurse_id, sfs_nurse_id)
                VALUES (
                    ?,
                    (SELECT nurse_id FROM nurses WHERE name=?),
                    (SELECT nurse_id FROM nurses WHERE name=?)
                )
                """,
                (date_str, fsf_nurse, sfs_nurse),
            )

        self._run_command("add_assignment", _write)

    def modify_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str) -> None:
        normalized = self._history._normalize_date(weekend_start)
        date_str = normalized.strftime("%Y-%m-%d")

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE weekend_assignments
                SET fsf_nurse_id = (SELECT nurse_id FROM nurses WHERE name = ?),
                    sfs_nurse_id = (SELECT nurse_id FROM nurses WHERE name = ?)
                WHERE weekend_start = ?
                """,
                (fsf_nurse, sfs_nurse, date_str),
            )

        self._run_command("modify_assignment", _write)

    def remove_assignment(self, weekend_start) -> None:
        normalized = self._history._normalize_date(weekend_start)
        date_str = normalized.strftime("%Y-%m-%d")

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                DELETE FROM weekend_assignments
                WHERE weekend_start = ?
                """,
                (date_str,),
            )

        self._run_command("remove_assignment", _write)

    def rebuild(self) -> None:
        """Rebuild all derived weekend state from canonical weekend assignments."""

        def _noop(conn: sqlite3.Connection) -> None:
            _ = conn

        self._run_command("rebuild", _noop)

    def restore_assignments(
        self,
        backup_assignments: Iterable[tuple[pd.Timestamp, Optional[str], Optional[str]]],
    ) -> None:
        normalized_rows = []
        for weekend_start, fsf, sfs in backup_assignments:
            normalized = self._history._normalize_date(weekend_start)
            normalized_rows.append((normalized.strftime("%Y-%m-%d"), fsf, sfs))

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM weekend_assignments")
            for date_str, fsf, sfs in normalized_rows:
                conn.execute(
                    """
                    INSERT INTO weekend_assignments
                    (weekend_start, fsf_nurse_id, sfs_nurse_id)
                    VALUES (
                        ?,
                        (SELECT nurse_id FROM nurses WHERE name = ?),
                        (SELECT nurse_id FROM nurses WHERE name = ?)
                    )
                    """,
                    (date_str, fsf, sfs),
                )

        self._run_command("restore", _write)

    def _run_command(self, command_name: str, canonical_write) -> None:
        with sqlite3.connect(self._history.db_name) as conn:
            try:
                conn.execute("BEGIN")
                canonical_write(conn)
                chronological_assignments = self._load_chronological_assignments(conn)
                self._history._rebuild_rotation_history(conn, chronological_assignments)
                self._history._rebuild_violation_tables(conn, chronological_assignments)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("WeekendHistoryService command failed: %s", command_name)
                raise

        self._history._assignments = self._history._load_assignments()
        self._history._rebuild_last_patterns()

    def _load_chronological_assignments(
        self,
        conn: sqlite3.Connection,
    ) -> list[tuple[pd.Timestamp, tuple[Optional[str], Optional[str]]]]:
        rows = conn.execute(
            """
            SELECT wa.weekend_start, nf.name, ns.name
            FROM weekend_assignments wa
            LEFT JOIN nurses nf ON wa.fsf_nurse_id = nf.nurse_id
            LEFT JOIN nurses ns ON wa.sfs_nurse_id = ns.nurse_id
            ORDER BY wa.weekend_start
            """
        ).fetchall()

        return [
            (self._history._normalize_date(weekend_start), (fsf, sfs))
            for weekend_start, fsf, sfs in rows
        ]


class ViolationHistoryService:
    """Command-style writes for violation history state."""

    def __init__(self, history: "WeekendHistory"):
        self._history = history

    def add_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str) -> None:
        self._history.weekend_service.add_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def modify_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str) -> None:
        self._history.weekend_service.modify_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def remove_assignment(self, weekend_start) -> None:
        self._history.weekend_service.remove_assignment(weekend_start)

    def rebuild(self) -> None:
        """Rebuild violation tables from canonical weekend assignments."""
        with sqlite3.connect(self._history.db_name) as conn:
            try:
                conn.execute("BEGIN")
                self._history._assignments = self._history._load_assignments()
                chronological_assignments = sorted(
                    self._history._assignments.items(),
                    key=lambda assignment: assignment[0],
                )
                self._history._rebuild_violation_tables(conn, chronological_assignments)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("ViolationHistoryService rebuild failed")
                raise
