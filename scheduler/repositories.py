"""Database-backed repositories and canonical weekend-history aggregate."""

from __future__ import annotations

import copy
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from .domain import WeekendPattern
from .history_services import ViolationHistoryService, WeekendHistoryService

logger = logging.getLogger(__name__)


_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure_schema(db_name: str) -> None:
    """
    Create every table and index the application expects, if missing.

    The statements in ``schema.sql`` are all ``IF NOT EXISTS``, so this is
    idempotent and safe to call on each repository construction. Running it up
    front is what lets a fresh checkout bootstrap an empty database instead of
    relying on a pre-populated file being checked into the repository.
    """
    with sqlite3.connect(db_name) as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


class DatabaseMixin:
    """Mixin class to provide common database operations."""

    def __init__(self, db_name: str = "nurse_schedule.db"):
        self.db_name = db_name
        ensure_schema(db_name)

    @contextmanager
    def get_db_connection(self):
        """Context manager for database connections with error handling."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name)
            try:
                # Enforce foreign keys so future writes don't create orphans
                conn.execute("PRAGMA foreign_keys = ON")
            except Exception:
                # Older SQLite builds can be quirky—fail open rather than crash
                pass
            yield conn
        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            if conn:
                conn.rollback()
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.commit()
                conn.close()

    def execute_query(self, query: str, params: tuple = ()) -> list[tuple]:
        """Execute a SELECT query and return all results."""
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute_single_query(self, query: str, params: tuple = ()) -> tuple | None:
        """Execute a SELECT query and return single result."""
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()

    def execute_update(self, query: str, params: tuple = ()) -> None:
        """Execute an INSERT/UPDATE/DELETE query."""
        with self.get_db_connection() as conn:
            conn.execute(query, params)
            conn.commit()


class DateUtils:
    """Utility class for date operations."""

    @staticmethod
    def normalize_date(date_input) -> pd.Timestamp:
        """Normalize input date to pandas Timestamp at midnight."""
        try:
            return pd.to_datetime(date_input).normalize()
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to normalize date: {date_input}, error: {e}")
            raise ValueError(f"Invalid date format: {date_input}") from e

    @staticmethod
    def safe_normalize_date(date_input) -> pd.Timestamp | None:
        """Safely normalize input date, returning None on failure."""
        try:
            normalized = DateUtils.normalize_date(date_input)
        except ValueError:
            logger.warning(f"Failed to normalize date: {date_input}")
            return None
        if pd.isna(normalized):
            logger.warning(f"Date normalization produced NaT for input: {date_input}")
            return None
        return normalized


class AssignmentHistory(DatabaseMixin):
    """Manages nurse assignment history with database persistence and caching."""

    # SQL queries as class constants
    LOAD_HISTORY_QUERY = """
        SELECT sh.date, nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id=nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id=nb.nurse_id
        WHERE sh.date >= ?
    """

    GET_HISTORY_BASE_QUERY = """
        SELECT sh.date, nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id = nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id = nb.nurse_id
    """

    UPDATE_HISTORY_QUERY = """
        INSERT OR REPLACE INTO schedule_history (date, main_nurse_id, backup_nurse_id)
        VALUES (?,
                (SELECT nurse_id FROM nurses WHERE name=?),
                (SELECT nurse_id FROM nurses WHERE name=?))
    """

    GET_RECORD_QUERY = """
        SELECT nm.name, nb.name
        FROM schedule_history sh
        LEFT JOIN nurses nm ON sh.main_nurse_id=nm.nurse_id
        LEFT JOIN nurses nb ON sh.backup_nurse_id=nb.nurse_id
        WHERE sh.date=?
    """

    def __init__(self, db_name: str = "nurse_schedule.db", history_duration_months: int = 6):
        super().__init__(db_name)
        self.history_duration_months = history_duration_months
        self._history = self._load_history()

    def _get_cutoff_date(self) -> pd.Timestamp:
        """Calculate the cutoff date for history retention."""
        return DateUtils.normalize_date(
            pd.Timestamp.today() - pd.DateOffset(months=self.history_duration_months)
        )

    def _load_history(self) -> dict[pd.Timestamp, dict[str, str | None]]:
        """Load assignment history from the database."""
        history = {}
        cutoff_date = self._get_cutoff_date()
        cutoff_date_str = cutoff_date.strftime("%Y-%m-%d")

        results = self.execute_query(self.LOAD_HISTORY_QUERY, (cutoff_date_str,))

        for date_str, main, backup in results:
            normalized_date = DateUtils.normalize_date(date_str)
            history[normalized_date] = {"main": main, "backup": backup}

        return history

    def _refresh_cache(self) -> None:
        """Refresh the in-memory cache from database."""
        self._history = self._load_history()

    def reload(self) -> None:
        """Re-read history written by other instances (for example, an apply)."""
        self._refresh_cache()

    def get_all_history(self) -> list[tuple[str, str | None, str | None]]:
        """Return all history records as a list of tuples."""
        return [
            (date.strftime("%Y-%m-%d"), data["main"], data["backup"])
            for date, data in sorted(self._history.items())
        ]

    def get_history(
        self, start_date=None, end_date=None
    ) -> list[tuple[str, str | None, str | None]]:
        """
        Retrieve assignment history records within an optional date range.
        Args:
            start_date: Optional start date (inclusive). If None, no lower bound.
            end_date: Optional end date (inclusive). If None, no upper bound.
        Returns:
            List of tuples containing (date_str, main_nurse, backup_nurse).
        """
        query = self.GET_HISTORY_BASE_QUERY
        conditions = []
        params = []

        if start_date:
            start = DateUtils.normalize_date(start_date).strftime("%Y-%m-%d")
            conditions.append("sh.date >= ?")
            params.append(start)

        if end_date:
            end = DateUtils.normalize_date(end_date).strftime("%Y-%m-%d")
            conditions.append("sh.date <= ?")
            params.append(end)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY sh.date ASC"

        results = self.execute_query(query, params)
        return [(date_str, main, backup) for date_str, main, backup in results]

    def get_counts(self, start_date, end_date) -> tuple[dict[str, int], dict[str, int]]:
        """Get counts of main and backup assignments within a date range."""
        main_counts = {}
        backup_counts = {}
        start = DateUtils.normalize_date(start_date)
        end = DateUtils.normalize_date(end_date)

        for date, data in self._history.items():
            if start <= date <= end:
                main = data["main"]
                backup = data["backup"]
                if main:
                    main_counts[main] = main_counts.get(main, 0) + 1
                if backup:
                    backup_counts[backup] = backup_counts.get(backup, 0) + 1

        return main_counts, backup_counts

    def update_history(self, date_input, main_nurse: str | None, backup_nurse: str | None) -> None:
        """Insert or update an assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime("%Y-%m-%d")

        self.execute_update(self.UPDATE_HISTORY_QUERY, (date_str, main_nurse, backup_nurse))

        # Update in-memory cache
        self._history[normalized_date] = {"main": main_nurse, "backup": backup_nurse}

    def get_record(self, date_input) -> tuple[str | None, str | None] | None:
        """Retrieve a specific assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime("%Y-%m-%d")

        return self.execute_single_query(self.GET_RECORD_QUERY, (date_str,))

    def delete_record(self, date_input) -> None:
        """Delete a specific assignment record."""
        normalized_date = DateUtils.normalize_date(date_input)
        date_str = normalized_date.strftime("%Y-%m-%d")

        self.execute_update("DELETE FROM schedule_history WHERE date=?", (date_str,))

        # Update in-memory cache
        self._history.pop(normalized_date, None)

    def prune_old_records(self) -> None:
        """Remove records older than the cutoff date."""
        cutoff_date = self._get_cutoff_date()
        cutoff_date_str = cutoff_date.strftime("%Y-%m-%d")

        self.execute_update("DELETE FROM schedule_history WHERE date < ?", (cutoff_date_str,))

        # Update in-memory cache
        self._history = {date: data for date, data in self._history.items() if date >= cutoff_date}


class NurseManager(DatabaseMixin):
    """Manages nurse data and availability with database persistence and caching."""

    # SQL queries as class constants
    LOAD_NURSES_QUERY = "SELECT nurse_id, name, is_prn, is_late_shift FROM nurses WHERE is_active=1"
    LOAD_UNAVAILABLE_DATES_QUERY = "SELECT nurse_id, date FROM unavailable_dates"
    GET_NURSE_UNAVAILABLE_DATES_QUERY = """
        SELECT date FROM unavailable_dates
        WHERE nurse_id=(SELECT nurse_id FROM nurses WHERE name=?)
    """

    def __init__(self, db_name: str = "nurse_schedule.db"):
        super().__init__(db_name)
        self._ensure_is_active_column()
        self._nurses = self._load_nurses()

    def _load_nurses(self) -> dict[str, dict[str, Any]]:
        """Load nurses and their unavailable dates from the database."""
        nurses_dict = {}

        # Load nurse basic information
        nurse_results = self.execute_query(self.LOAD_NURSES_QUERY)
        nurse_ids = {}

        for nurse_id, name, is_prn, is_late_shift in nurse_results:
            nurses_dict[name] = {
                "nurse_id": nurse_id,
                "is_prn": bool(is_prn),
                "is_late_shift": bool(is_late_shift),
                "unavailable_dates": set(),
            }
            nurse_ids[nurse_id] = name

        # Load unavailable dates
        unavailable_results = self.execute_query(self.LOAD_UNAVAILABLE_DATES_QUERY)

        for nurse_id, date_str in unavailable_results:
            nurse_name = nurse_ids.get(nurse_id)
            if nurse_name:
                date_obj = DateUtils.safe_normalize_date(date_str)
                if date_obj:
                    nurses_dict[nurse_name]["unavailable_dates"].add(date_obj)

        return nurses_dict

    def refresh_cache(self) -> None:
        """Force reload of nurse data from database."""
        self._nurses = self._load_nurses()
        logger.info("Nurse manager cache refreshed")

    def reload(self) -> None:
        """
        Re-query the database and rebuild the internal nurse dictionary.
        Call this after any SQL that may have changed the nurses table.
        """
        self.refresh_cache()

    def get_non_prn_nurses(self) -> list[str]:
        """Get list of non-PRN nurses in a stable, deterministic order."""
        results = self.execute_query(
            "SELECT name FROM nurses WHERE is_prn=0 AND is_active=1 ORDER BY name COLLATE NOCASE"
        )
        return [row[0] for row in results]

    def get_prn_nurses(self) -> list[str]:
        """Get list of PRN nurses in a stable, deterministic order."""
        results = self.execute_query(
            "SELECT name FROM nurses WHERE is_prn=1 AND is_active=1 ORDER BY name COLLATE NOCASE"
        )
        return [row[0] for row in results]

    def get_nurses(self) -> list[str]:
        """Get all active nurses in a stable, deterministic order."""
        results = self.execute_query(
            "SELECT name FROM nurses WHERE is_active=1 ORDER BY name COLLATE NOCASE"
        )
        return [row[0] for row in results]

    @property
    def nurses(self) -> dict[str, dict[str, Any]]:
        """Get the nurses dictionary."""
        return self._nurses

    def is_prn_nurse(self, nurse: str) -> bool:
        """Check if a nurse is PRN (as needed)."""
        return self._nurses.get(nurse, {}).get("is_prn", False)

    def is_late_shift_nurse(self, nurse: str) -> bool:
        """Check if a nurse works late shifts."""
        return self._nurses.get(nurse, {}).get("is_late_shift", False)

    def get_unavailable_dates(self, nurse: str) -> set[pd.Timestamp]:
        """Get a nurse's unavailable dates."""
        return self._nurses.get(nurse, {}).get("unavailable_dates", set())

    def find_nurse(self, name: str) -> dict[str, Any] | None:
        """Look a nurse up by name, ignoring case and including inactive rows.

        Returns ``{"name", "is_active", "is_prn", "is_late_shift"}`` or None.
        """
        rows = self.execute_query(
            "SELECT name, is_active, is_prn, is_late_shift FROM nurses "
            "WHERE name = ? COLLATE NOCASE",
            (name.strip(),),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "name": row[0],
            "is_active": bool(row[1]),
            "is_prn": bool(row[2]),
            "is_late_shift": bool(row[3]),
        }

    def add_nurse(self, name: str, is_prn: bool = False, is_late_shift: bool = False) -> None:
        """Add or reactivate a nurse. If the nurse exists, mark active and update flags."""
        # Insert if new; ignore if exists
        self.execute_update(
            "INSERT OR IGNORE INTO nurses (name, is_prn, is_late_shift) VALUES (?, ?, ?)",
            (name, is_prn, is_late_shift),
        )
        # Ensure active + sync flags (works for both new and existing rows)
        self.execute_update(
            "UPDATE nurses SET is_active=1, is_prn=?, is_late_shift=? WHERE name=?",
            (is_prn, is_late_shift, name),
        )
        self.refresh_cache()

    def remove_nurse(self, name: str, hard_delete: bool = False) -> None:
        """
        Deactivate a nurse by default (soft delete). If hard_delete=True,
        also clean up references and drop the row.
        """
        if hard_delete:
            self._hard_delete_nurse_and_cleanup(name)
            return
        # Soft-delete
        self.execute_update("UPDATE nurses SET is_active=0 WHERE name=?", (name,))
        self._nurses.pop(name, None)

    def set_prn_status(self, name: str, is_prn: bool) -> None:
        """Update PRN status for a nurse."""
        self.execute_update("UPDATE nurses SET is_prn=? WHERE name=?", (is_prn, name))
        if name in self._nurses:
            self._nurses[name]["is_prn"] = is_prn

    def set_late_shift_status(self, name: str, is_late_shift: bool) -> None:
        """Update late shift status for a nurse."""
        self.execute_update("UPDATE nurses SET is_late_shift=? WHERE name=?", (is_late_shift, name))
        if name in self._nurses:
            self._nurses[name]["is_late_shift"] = is_late_shift

    def update_unavailable_dates(self, nurse_name: str, new_dates: set) -> None:
        """Update unavailable dates for a nurse in the database and internal cache."""

        if not nurse_name:
            logger.warning("No nurse name provided for updating unavailable dates")
            return

        # Normalize incoming dates to midnight
        processed_dates: set[pd.Timestamp] = set()
        for d in new_dates:
            d_obj = DateUtils.safe_normalize_date(d)
            if d_obj:
                processed_dates.add(d_obj)

        # Pull current dates from DB to compute deltas
        current_results = self.execute_query(self.GET_NURSE_UNAVAILABLE_DATES_QUERY, (nurse_name,))
        current_dates = set()
        for (date_str,) in current_results:
            d_obj = DateUtils.safe_normalize_date(date_str)
            if d_obj:
                current_dates.add(d_obj)

        dates_to_add = processed_dates - current_dates
        dates_to_remove = current_dates - processed_dates

        with self.get_db_connection() as conn:
            for date_obj in dates_to_add:
                conn.execute(
                    """INSERT OR IGNORE INTO unavailable_dates (nurse_id, date)
                       VALUES ((SELECT nurse_id FROM nurses WHERE name=?), ?)""",
                    (nurse_name, date_obj.date().strftime("%Y-%m-%d")),
                )

            for date_obj in dates_to_remove:
                conn.execute(
                    """DELETE FROM unavailable_dates
                       WHERE nurse_id=(SELECT nurse_id FROM nurses WHERE name=?) AND date=?""",
                    (nurse_name, date_obj.date().strftime("%Y-%m-%d")),
                )

            conn.commit()

        # Update cache
        if nurse_name in self._nurses:
            self._nurses[nurse_name]["unavailable_dates"] = processed_dates

    def get_prn_status(self, name: str) -> bool:
        """Get PRN status for a nurse."""
        return self.is_prn_nurse(name)

    def get_late_shift_status(self, name: str) -> bool:
        """Get late shift status for a nurse."""
        return self.is_late_shift_nurse(name)

    # --- New helpers for soft-delete / cleanup --------------------------------
    def activate_nurse(self, name: str) -> None:
        """Reactivate a previously deactivated nurse."""
        self.execute_update("UPDATE nurses SET is_active=1 WHERE name=?", (name,))
        self.refresh_cache()

    def deactivate_nurse(self, name: str) -> None:
        """Deactivate a nurse (soft delete)."""
        self.execute_update("UPDATE nurses SET is_active=0 WHERE name=?", (name,))
        self._nurses.pop(name, None)

    def _hard_delete_nurse_and_cleanup(self, name: str) -> None:
        """Hard-delete a nurse and clean up or null references in related tables."""
        with self.get_db_connection() as conn:
            cur = conn.execute("SELECT nurse_id FROM nurses WHERE name=?", (name,))
            row = cur.fetchone()
            if not row:
                return
            nurse_id = row[0]

            # Null out schedule_history references
            conn.execute(
                """
                UPDATE schedule_history
                SET main_nurse_id = CASE WHEN main_nurse_id = ? THEN NULL ELSE main_nurse_id END,
                    backup_nurse_id = CASE WHEN backup_nurse_id = ? THEN NULL ELSE backup_nurse_id END
            """,
                (nurse_id, nurse_id),
            )

            # Null out weekend_assignments references (if table exists)
            conn.execute(
                """
                UPDATE weekend_assignments
                SET fsf_nurse_id = CASE WHEN fsf_nurse_id = ? THEN NULL ELSE fsf_nurse_id END,
                    sfs_nurse_id = CASE WHEN sfs_nurse_id = ? THEN NULL ELSE sfs_nurse_id END
            """,
                (nurse_id, nurse_id),
            )

            # Remove availability + derived violation tables
            conn.execute("DELETE FROM unavailable_dates WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM rotation_violation_dates WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM rotation_violation_stats WHERE nurse_id = ?", (nurse_id,))
            conn.execute("DELETE FROM weekend_rotation_history WHERE nurse_id = ?", (nurse_id,))

            # Finally remove nurse
            conn.execute("DELETE FROM nurses WHERE nurse_id = ?", (nurse_id,))
            conn.commit()
        self._nurses.pop(name, None)

    def cleanup_orphan_rows(self) -> None:
        """One-shot cleanup to reconcile older DBs with missing FKs."""
        with self.get_db_connection() as conn:
            conn.execute("""
                UPDATE schedule_history
                SET main_nurse_id = NULL
                WHERE main_nurse_id IS NOT NULL
                  AND main_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE schedule_history
                SET backup_nurse_id = NULL
                WHERE backup_nurse_id IS NOT NULL
                  AND backup_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE weekend_assignments
                SET fsf_nurse_id = NULL
                WHERE fsf_nurse_id IS NOT NULL
                  AND fsf_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute("""
                UPDATE weekend_assignments
                SET sfs_nurse_id = NULL
                WHERE sfs_nurse_id IS NOT NULL
                  AND sfs_nurse_id NOT IN (SELECT nurse_id FROM nurses)
            """)
            conn.execute(
                "DELETE FROM unavailable_dates WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)"
            )
            conn.execute(
                "DELETE FROM rotation_violation_dates WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)"
            )
            conn.execute(
                "DELETE FROM rotation_violation_stats WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)"
            )
            conn.execute(
                "DELETE FROM weekend_rotation_history WHERE nurse_id NOT IN (SELECT nurse_id FROM nurses)"
            )
            conn.commit()

    def _ensure_is_active_column(self) -> None:
        """Add is_active column to nurses if missing."""
        with self.get_db_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(nurses)").fetchall()]
            if "is_active" not in cols:
                conn.execute("ALTER TABLE nurses ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
                conn.commit()


class DBTables:
    WEEKEND_ASSIGNMENTS = "weekend_assignments"
    ROTATION_VIOLATION_DATES = "rotation_violation_dates"
    ROTATION_VIOLATION_STATS = "rotation_violation_stats"
    WEEKEND_ROTATION_HISTORY = "weekend_rotation_history"
    NURSES = "nurses"


class DBColumns:
    NURSE_ID = "nurse_id"
    NAME = "name"
    WEEKEND_START = "weekend_start"
    FSF_NURSE_ID = "fsf_nurse_id"
    SFS_NURSE_ID = "sfs_nurse_id"
    VIOLATION_DATE = "violation_date"
    PATTERN = "pattern"
    PREVIOUS_PATTERN = "previous_pattern"
    VIOLATION_COUNT = "violation_count"
    LAST_VIOLATION_DATE = "last_violation_date"
    CONSEC_VIOLATIONS = "consec_violations"
    LAST_PATTERN = "last_pattern"
    EXPECTED_NEXT_PATTERN = "expected_next_pattern"


class WeekendHistory:
    def __init__(self, db_name: str = "nurse_schedule.db"):
        self.db_name = db_name
        ensure_schema(self.db_name)
        self._assignments = self._load_assignments()
        self._last_patterns = self._load_last_patterns()
        self.weekend_service = WeekendHistoryService(self)
        self.violation_service = ViolationHistoryService(self)

    # Utility Methods
    def _normalize_date(self, date_input) -> pd.Timestamp:
        """Normalize input date to pandas Timestamp at midnight."""
        return DateUtils.normalize_date(date_input)

    # Data Loading Methods
    def reload(self) -> None:
        """Re-read assignments and last patterns written by other instances."""
        self._assignments = self._load_assignments()
        self._last_patterns = self._load_last_patterns()

    def _load_assignments(self) -> dict[pd.Timestamp, tuple[str | None, str | None]]:
        """Load weekend assignments from the database with normalized timestamps."""
        assignments = {}
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(f"""
                SELECT {DBColumns.WEEKEND_START}, nf.{DBColumns.NAME}, ns.{DBColumns.NAME}
                FROM {DBTables.WEEKEND_ASSIGNMENTS}
                LEFT JOIN {DBTables.NURSES} nf ON {DBColumns.FSF_NURSE_ID} = nf.{DBColumns.NURSE_ID}
                LEFT JOIN {DBTables.NURSES} ns ON {DBColumns.SFS_NURSE_ID} = ns.{DBColumns.NURSE_ID}
            """)
            for weekend_start, fsf, sfs in cursor.fetchall():
                normalized_date = self._normalize_date(weekend_start)
                assignments[normalized_date] = (fsf, sfs)
        return assignments

    def _load_last_patterns(self) -> dict[str, WeekendPattern | None]:
        """Load last patterns for all nurses from database."""
        patterns: dict[str, WeekendPattern | None] = {}
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(f"""
                SELECT n.{DBColumns.NAME}, wrh.{DBColumns.LAST_PATTERN}
                FROM {DBTables.WEEKEND_ROTATION_HISTORY} wrh
                JOIN {DBTables.NURSES} n ON wrh.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            """)
            for nurse, pattern_str in cursor.fetchall():
                patterns[nurse] = self._parse_weekend_pattern(pattern_str)
        return patterns

    def _parse_weekend_pattern(self, pattern_str: str | None) -> WeekendPattern | None:
        """Parse weekend pattern string safely."""
        if pattern_str is None:
            return None
        try:
            return WeekendPattern(pattern_str)
        except ValueError:
            return None

    # Violation Management Methods
    def get_violation_dates(self, nurse: str = None) -> list[tuple]:
        """Get violation dates for a nurse or all nurses."""
        with sqlite3.connect(self.db_name) as conn:
            if nurse:
                cursor = conn.execute(
                    f"""
                    SELECT n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE},
                           vd.{DBColumns.PATTERN}, vd.{DBColumns.PREVIOUS_PATTERN}
                    FROM {DBTables.ROTATION_VIOLATION_DATES} vd
                    JOIN {DBTables.NURSES} n ON vd.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
                    WHERE n.{DBColumns.NAME} = ?
                    ORDER BY vd.{DBColumns.VIOLATION_DATE} ASC
                """,
                    (nurse,),
                )
            else:
                cursor = conn.execute(f"""
                    SELECT n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE},
                           vd.{DBColumns.PATTERN}, vd.{DBColumns.PREVIOUS_PATTERN}
                    FROM {DBTables.ROTATION_VIOLATION_DATES} vd
                    JOIN {DBTables.NURSES} n ON vd.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
                    ORDER BY n.{DBColumns.NAME}, vd.{DBColumns.VIOLATION_DATE} ASC
                """)
            return cursor.fetchall()

    def _calculate_consecutive_violations(
        self,
        last_violation_date: pd.Timestamp | None,
        current_violation_date: pd.Timestamp,
        current_streak: int,
    ) -> int:
        """Calculate consecutive violation count."""
        if last_violation_date is None:
            return 1
        delta_days = (current_violation_date - last_violation_date).days
        return current_streak + 1 if delta_days == 7 else 1

    def _build_nurse_sequences(self) -> dict[str, list[tuple[pd.Timestamp, WeekendPattern]]]:
        """Build chronological sequences of assignments for each nurse."""
        return self._build_nurse_sequences_from_assignments(sorted(self._assignments.items()))

    def _build_nurse_sequences_from_assignments(
        self,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[str | None, str | None]]],
    ) -> dict[str, list[tuple[pd.Timestamp, WeekendPattern]]]:
        """Build chronological sequences of assignments for each nurse."""
        seq_per_nurse: dict[str, list[tuple[pd.Timestamp, WeekendPattern]]] = {}

        for wk_start, (fsf, sfs) in chronological_assignments:
            if fsf:
                seq_per_nurse.setdefault(fsf, []).append((wk_start, WeekendPattern.FSF))
            if sfs:
                seq_per_nurse.setdefault(sfs, []).append((wk_start, WeekendPattern.SFS))

        return seq_per_nurse

    def _process_nurse_violations(
        self, nurse: str, sequence: list[tuple[pd.Timestamp, WeekendPattern]]
    ) -> tuple[list, int, pd.Timestamp | None, int]:
        """Process violations for a single nurse's sequence."""
        violation_dates = []
        violation_count = 0
        last_violation_date = None
        streak = 0
        prev_violation_date = None

        for i in range(1, len(sequence)):
            prev_date, prev_pat = sequence[i - 1]
            curr_date, curr_pat = sequence[i]

            if prev_pat == curr_pat:  # Violation detected
                violation_count += 1
                violation_dates.append((curr_date, curr_pat, prev_pat))
                last_violation_date = curr_date

                # Calculate consecutive streak
                if prev_violation_date is not None and (curr_date - prev_violation_date).days == 7:
                    streak += 1
                else:
                    streak = 1
                prev_violation_date = curr_date
            else:
                streak = 0

        return violation_dates, violation_count, last_violation_date, streak

    def _write_violation_dates_to_db(self, conn, nurse: str, violation_dates: list):
        """Write violation dates to database."""
        for vdate, vpat, vprev in violation_dates:
            date_str = vdate.strftime("%Y-%m-%d")
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {DBTables.ROTATION_VIOLATION_DATES}
                ({DBColumns.NURSE_ID}, {DBColumns.VIOLATION_DATE},
                 {DBColumns.PATTERN}, {DBColumns.PREVIOUS_PATTERN})
                VALUES (
                    (SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME} = ?),
                    ?, ?, ?
                )
            """,
                (nurse, date_str, vpat.value, vprev.value),
            )

    def _update_nurse_violation_stats(
        self,
        conn,
        nurse: str,
        violation_count: int,
        last_violation_date: pd.Timestamp | None,
        streak: int,
    ):
        """Update violation stats for a nurse."""
        last_date_str = last_violation_date.strftime("%Y-%m-%d") if last_violation_date else None

        conn.execute(
            f"""
            INSERT INTO {DBTables.ROTATION_VIOLATION_STATS}
                  ({DBColumns.NURSE_ID}, {DBColumns.VIOLATION_COUNT},
                   {DBColumns.LAST_VIOLATION_DATE}, {DBColumns.CONSEC_VIOLATIONS})
            VALUES ((SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME}=?), ?, ?, ?)
            ON CONFLICT({DBColumns.NURSE_ID}) DO UPDATE
            SET {DBColumns.VIOLATION_COUNT} = excluded.{DBColumns.VIOLATION_COUNT},
                {DBColumns.LAST_VIOLATION_DATE} = excluded.{DBColumns.LAST_VIOLATION_DATE},
                {DBColumns.CONSEC_VIOLATIONS} = excluded.{DBColumns.CONSEC_VIOLATIONS}
        """,
            (nurse, violation_count, last_date_str, streak),
        )

    def _rebuild_rotation_history(
        self,
        conn,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[str | None, str | None]]],
    ) -> None:
        """Rebuild weekend rotation history table from chronological assignments."""
        conn.execute(f"DELETE FROM {DBTables.WEEKEND_ROTATION_HISTORY}")

        for _, (fsf, sfs) in chronological_assignments:
            if fsf:
                self._write_pattern(conn, fsf, WeekendPattern.FSF)
            if sfs:
                self._write_pattern(conn, sfs, WeekendPattern.SFS)

    def _rebuild_violation_tables(
        self,
        conn,
        chronological_assignments: list[tuple[pd.Timestamp, tuple[str | None, str | None]]],
    ) -> None:
        """Rebuild violation dates and stats from chronological assignments."""
        conn.execute(f"DELETE FROM {DBTables.ROTATION_VIOLATION_DATES}")
        conn.execute(f"DELETE FROM {DBTables.ROTATION_VIOLATION_STATS}")

        seq_per_nurse = self._build_nurse_sequences_from_assignments(chronological_assignments)

        for nurse, sequence in seq_per_nurse.items():
            (
                violation_dates,
                violation_count,
                last_violation_date,
                streak,
            ) = self._process_nurse_violations(nurse, sequence)

            self._write_violation_dates_to_db(conn, nurse, violation_dates)
            self._update_nurse_violation_stats(
                conn, nurse, violation_count, last_violation_date, streak
            )

    def _rebuild_last_patterns(self) -> None:
        """Reload in-memory last patterns from weekend rotation history table."""
        self._last_patterns = self._load_last_patterns()

    def _recalculate_violation_counts(self) -> None:
        """Rebuild violation tables from canonical assignments."""
        self.violation_service.rebuild()

    def _rebuild_derived_weekend_state(self) -> None:
        """Rebuild all derived weekend state from canonical weekend assignments."""
        self.weekend_service.rebuild()

    # Pattern Management Methods
    def _write_pattern(self, conn, nurse: str, new_pat: WeekendPattern) -> None:
        """Write pattern to database."""
        expected_next = WeekendPattern.FSF if new_pat == WeekendPattern.SFS else WeekendPattern.SFS

        conn.execute(
            f"""
            INSERT INTO {DBTables.WEEKEND_ROTATION_HISTORY}
            ({DBColumns.NURSE_ID}, {DBColumns.LAST_PATTERN}, {DBColumns.EXPECTED_NEXT_PATTERN})
            VALUES (
                (SELECT {DBColumns.NURSE_ID} FROM {DBTables.NURSES} WHERE {DBColumns.NAME}=?),
                ?, ?
            )
            ON CONFLICT({DBColumns.NURSE_ID}) DO UPDATE
            SET {DBColumns.LAST_PATTERN}=excluded.{DBColumns.LAST_PATTERN},
                {DBColumns.EXPECTED_NEXT_PATTERN}=excluded.{DBColumns.EXPECTED_NEXT_PATTERN}
        """,
            (nurse, new_pat.value, expected_next.value),
        )

    # Violation Statistics Methods
    def get_violation_summary(self, as_of=None):
        """Returns a DataFrame with violation summary for all nurses."""
        if as_of is None:
            as_of = pd.Timestamp.today().normalize()

        with sqlite3.connect(self.db_name) as conn:
            stats = pd.read_sql(
                f"""
                SELECT n.{DBColumns.NAME} as nurse,
                       COALESCE(vs.{DBColumns.VIOLATION_COUNT}, 0) as total_viol,
                       vs.{DBColumns.LAST_VIOLATION_DATE},
                       COALESCE(vs.{DBColumns.CONSEC_VIOLATIONS}, 0) as consec_viol
                FROM {DBTables.NURSES} n
                LEFT JOIN {DBTables.ROTATION_VIOLATION_STATS} vs
                  ON vs.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            """,
                conn,
                parse_dates=[DBColumns.LAST_VIOLATION_DATE],
            )

        stats = stats.fillna(
            {"total_viol": 0, DBColumns.LAST_VIOLATION_DATE: pd.NaT, "consec_viol": 0}
        )

        def calculate_clean_run_weeks(row):
            """Calculate clean run weeks for a nurse."""
            last = row[DBColumns.LAST_VIOLATION_DATE]
            if pd.isna(last):
                return 999  # Never violated

            all_wks = self.get_weekends(row["nurse"])
            future = [wk for wk in all_wks if wk > last and wk <= as_of]

            viol_dates = {
                DateUtils.normalize_date(r[1]) for r in self.get_violation_dates(row["nurse"])
            }

            clean_weeks = 0
            for wk in sorted(future):
                if wk not in viol_dates:
                    clean_weeks += 1
                else:
                    break
            return clean_weeks

        stats["clean_run_weeks"] = stats.apply(calculate_clean_run_weeks, axis=1).astype(int)
        stats["days_since_last"] = (
            (as_of - stats[DBColumns.LAST_VIOLATION_DATE]).dt.days.fillna(999).astype(int)
        )

        return stats

    def get_violation_counts(self) -> dict[str, int]:
        """Get violation counts for all nurses."""
        with sqlite3.connect(self.db_name) as conn:
            cur = conn.execute(f"""
                SELECT n.{DBColumns.NAME}, COALESCE(vs.{DBColumns.VIOLATION_COUNT}, 0)
                FROM {DBTables.NURSES} n
                LEFT JOIN {DBTables.ROTATION_VIOLATION_STATS} vs
                       ON vs.{DBColumns.NURSE_ID} = n.{DBColumns.NURSE_ID}
            """)
            return {name: cnt for name, cnt in cur.fetchall()}

    def get_violation_counts_before(self, before_date) -> dict[str, int]:
        """Each nurse's rotation violations on weekends before ``before_date``.

        A schedule starting on ``before_date`` replaces anything recorded from
        then on, so its violations are not the nurse's past. The stored count
        (which may be a manual ``set_violation_count`` override) is used for
        a nurse with no recorded violation on or after the date; otherwise
        their violations are recounted from the dates before it.
        """
        cutoff = self._normalize_date(before_date)
        stored = self.get_violation_counts()
        earlier: dict[str, int] = {}
        later: set[str] = set()
        for nurse, violation_date, _pattern, _previous in self.get_violation_dates():
            if self._normalize_date(violation_date) < cutoff:
                earlier[nurse] = earlier.get(nurse, 0) + 1
            else:
                later.add(nurse)
        return {
            nurse: (earlier.get(nurse, 0) if nurse in later else count)
            for nurse, count in stored.items()
        }

    # Public Interface Methods
    def get_last_weekend_before(self, nurse: str, before_date: pd.Timestamp) -> pd.Timestamp | None:
        """Get the last weekend assignment before a given date."""
        weekends = [w for w in self.get_weekends(nurse) if w < before_date]
        return max(weekends) if weekends else None

    def get_assignments(self) -> list[tuple[pd.Timestamp, str | None, str | None]]:
        """Get all assignments sorted by date."""
        return sorted(
            [(date, fsf, sfs) for date, (fsf, sfs) in self._assignments.items()], key=lambda x: x[0]
        )

    def get_last_pattern(self, nurse: str) -> WeekendPattern | None:
        """Get the last pattern for a nurse."""
        return self._last_patterns.get(nurse)

    def get_last_pattern_before(self, nurse: str, before_date) -> WeekendPattern | None:
        """The pattern of the nurse's last recorded weekend before ``before_date``.

        A schedule generated from ``before_date`` must alternate against the
        weekend before it, not against weekends recorded inside or after its
        own window (for example, an earlier run of the same month being
        replaced). The stored last pattern, which may be a manual
        ``set_last_pattern`` override, is used only when the nurse has no
        recorded weekend on or after ``before_date``. Otherwise the override
        predates those weekends and no longer describes the nurse.
        """
        cutoff = self._normalize_date(before_date)
        last_before: tuple[pd.Timestamp, WeekendPattern] | None = None
        worked_later = False
        for weekend_start, (fsf, sfs) in self._assignments.items():
            if nurse == fsf:
                pattern = WeekendPattern.FSF
            elif nurse == sfs:
                pattern = WeekendPattern.SFS
            else:
                continue
            if weekend_start >= cutoff:
                worked_later = True
            elif last_before is None or weekend_start > last_before[0]:
                last_before = (weekend_start, pattern)

        if not worked_later:
            return self._last_patterns.get(nurse)
        return last_before[1] if last_before else None

    def get_assignment(self, weekend_start) -> tuple[str | None, str | None] | None:
        """The recorded ``(fsf, sfs)`` pair for one weekend, or None."""
        return self._assignments.get(self._normalize_date(weekend_start))

    def get_weekends(self, nurse: str) -> list[pd.Timestamp]:
        """Get all weekend assignments for a nurse."""
        return [
            weekend_start
            for weekend_start, (fsf, sfs) in self._assignments.items()
            if nurse in (fsf, sfs)
        ]

    def backup(self) -> list[tuple[pd.Timestamp, str | None, str | None]]:
        """Create a backup of all assignments."""
        return copy.deepcopy(self.get_assignments())

    def restore(self, backup_assignments: list):
        """Restore assignments from backup."""
        self.weekend_service.restore_assignments(backup_assignments)

    def set_violation_count(self, nurse: str, count: int) -> None:
        """Manual override for a nurse's violation count.

        Persists until the next canonical-from-assignments rebuild.
        """
        self.violation_service.set_violation_count(nurse, count)

    def set_last_pattern(self, nurse: str, pattern: WeekendPattern) -> None:
        """Manual override for a nurse's last weekend pattern.

        Persists until the next canonical-from-assignments rebuild.
        """
        self.weekend_service.set_last_pattern(nurse, pattern)

    def add_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str):
        """Add a new weekend assignment."""
        self.weekend_service.add_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def modify_assignment(self, weekend_start, fsf_nurse: str, sfs_nurse: str):
        """Modify an existing weekend assignment."""
        self.weekend_service.modify_assignment(weekend_start, fsf_nurse, sfs_nurse)

    def remove_assignment(self, weekend_start):
        """Remove a weekend assignment."""
        self.weekend_service.remove_assignment(weekend_start)


class PreScheduler:
    def __init__(self, db_name="nurse_schedule.db"):
        self.db_name = db_name
        self._assignments = self._load_assignments()

    @staticmethod
    def _normalize_date(date_input) -> pd.Timestamp:
        """
        Convert *anything* (str / date / datetime / Timestamp) to a
        pandas.Timestamp normalised to 00:00.
        """
        return DateUtils.normalize_date(date_input)

    def _load_assignments(self) -> dict[pd.Timestamp, dict]:
        """
        Read the table once and keep it in memory.
        The dictionary key is now a **normalised pandas.Timestamp** so
        all later comparisons use the exact same representation.
        """
        assignments: dict[pd.Timestamp, dict] = {}

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute(
                """
                SELECT psa.date, nm.name, nb.name
                FROM   pre_scheduled_assignments psa
                LEFT   JOIN nurses nm ON psa.main_nurse_id  = nm.nurse_id
                LEFT   JOIN nurses nb ON psa.backup_nurse_id = nb.nurse_id
                """
            )
            for date_str, main, backup in cursor.fetchall():
                ts = self._normalize_date(date_str)
                assignments[ts] = {"main": main, "backup": backup}

        return assignments

    def get_assignments_in_range(
        self, start_date, end_date
    ) -> dict[pd.Timestamp, dict[str, str | None]]:
        """
        Inclusive filter on the in-memory dictionary.  The two boundary
        arguments can be str / datetime / Timestamp.
        """
        start = self._normalize_date(start_date)
        end = self._normalize_date(end_date)

        return {ts: info for ts, info in self._assignments.items() if start <= ts <= end}

    def add_assignment(self, date_str, main_nurse, backup_nurse, note=""):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pre_scheduled_assignments (date, main_nurse_id, backup_nurse_id, note)
                VALUES (?,
                        (SELECT nurse_id FROM nurses WHERE name=?),
                        (SELECT nurse_id FROM nurses WHERE name=?),
                        ?)
            """,
                (date_str, main_nurse, backup_nurse, note),
            )
        self._assignments = self._load_assignments()

    def remove_assignment(self, date_str):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("DELETE FROM pre_scheduled_assignments WHERE date=?", (date_str,))
        self._assignments = self._load_assignments()

    def get_assignments(self):
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.execute("""
                SELECT date, nm.name, nb.name, note
                FROM pre_scheduled_assignments
                LEFT JOIN nurses nm ON main_nurse_id=nm.nurse_id
                LEFT JOIN nurses nb ON backup_nurse_id=nb.nurse_id
            """)
            return cursor.fetchall()


__all__ = [
    "DatabaseMixin",
    "DateUtils",
    "AssignmentHistory",
    "NurseManager",
    "DBTables",
    "DBColumns",
    "WeekendHistory",
    "PreScheduler",
]
