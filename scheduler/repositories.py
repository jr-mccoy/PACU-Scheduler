"""This module owns database/repository primitives; legacy import remains temporary."""

from contextlib import contextmanager
import logging
import sqlite3
from typing import Optional

from .legacy_core import WeekendHistory, NurseManager, AssignmentHistory

logger = logging.getLogger(__name__)


class DatabaseMixin:
    """Mixin class to provide common database operations."""

    def __init__(self, db_name: str = "nurse_schedule.db"):
        self.db_name = db_name

    @contextmanager
    def get_db_connection(self):
        """Context manager for database connections with error handling."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
            except Exception:
                pass
            yield conn
        except sqlite3.Error as exc:
            logger.error(f"Database error: {exc}")
            if conn:
                conn.rollback()
            raise
        except Exception as exc:
            logger.error(f"Unexpected error: {exc}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.commit()
                conn.close()

    def execute_query(self, query: str, params: tuple = ()) -> list[tuple]:
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute_single_query(self, query: str, params: tuple = ()) -> Optional[tuple]:
        with self.get_db_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()

    def execute_update(self, query: str, params: tuple = ()) -> None:
        with self.get_db_connection() as conn:
            conn.execute(query, params)
            conn.commit()


__all__ = [
    "DatabaseMixin",
    "WeekendHistory",
    "NurseManager",
    "AssignmentHistory",
]
