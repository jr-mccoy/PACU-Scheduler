"""Backend composition for non-CLI consumers (GUI, services, tests).

The GUI historically used :class:`scheduler.legacy_core.NurseSchedulerUI`
(a CLI menu loop) as its "backend service object", which pulled
``input()``/``print()`` code into every GUI session. This module replaces
that pattern with :class:`SchedulerService` — a pure backend composition
of the repositories and scheduling helpers — and exposes
:func:`build_scheduler_service` as the single factory both the GUI and
CLI can call.

``BackendService`` defines the protocol the GUI now depends on; anything
beyond it (interactive prompts, terminal rendering) lives in the ``cli``
package.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional, Protocol, Tuple

import pandas as pd

from .domain import SchedulerConfig
from .engine import NurseScheduler
from .repositories import (
    AssignmentHistory,
    DateUtils,
    NurseManager,
    PreScheduler,
    WeekendHistory,
)
from .settings import SharedSettings

logger = logging.getLogger(__name__)


SyncReport = dict  # {"added": int, "conflicts_overwritten": int, "conflicts_skipped": int}


class BackendService(Protocol):
    """Public protocol that ``App.backend`` depends on.

    Any object exposing these attributes can be passed to the GUI in
    place of :class:`SchedulerService`. The protocol intentionally only
    lists the surface area the GUI actually touches today.
    """

    settings: SharedSettings
    nurse_manager: NurseManager
    pre_scheduler: PreScheduler
    weekend_history: WeekendHistory
    assignment_history: AssignmentHistory

    def sync_assignment_history_with_weekend(
        self,
        *,
        apply_additions: bool = True,
        overwrite_conflicts: bool = True,
    ) -> SyncReport: ...


class SchedulerService:
    """Plain composition of the backend objects the GUI talks to.

    Replaces the half of ``NurseSchedulerUI.__init__`` that wires
    repositories together, without the CLI menu surface. Methods on this
    class never call ``input()`` or ``print()``.
    """

    def __init__(self, db_name: str = "nurse_schedule.db") -> None:
        logger.info("Initializing SchedulerService with database: %s", db_name)
        self.db_name = db_name
        self.settings = SharedSettings()
        self.nurse_manager = NurseManager(db_name)
        self.pre_scheduler = PreScheduler(db_name)
        self.weekend_history = WeekendHistory(db_name)
        self.weekend_history_service = self.weekend_history.weekend_service
        self.violation_history_service = self.weekend_history.violation_service
        self.assignment_history = AssignmentHistory(db_name)

    # ------------------------------------------------------------------
    # Assignment-history / weekend-history sync (non-interactive)
    # ------------------------------------------------------------------
    def sync_assignment_history_with_weekend(
        self,
        *,
        apply_additions: bool = True,
        overwrite_conflicts: bool = True,
    ) -> SyncReport:
        """Reconcile assignment_history with weekend_history non-interactively.

        Returns a summary dict; raises on database errors so the GUI can
        surface them. This is the non-CLI replacement for
        ``NurseSchedulerUI._handle_sync_assignment_history_with_weekend``.
        """
        weekends = self.weekend_history.get_assignments()
        added = 0
        overwritten = 0
        skipped = 0

        for friday, fsf, sfs in weekends:
            friday_ts = DateUtils.normalize_date(friday)
            saturday = friday_ts + timedelta(days=1)
            sunday = friday_ts + timedelta(days=2)

            expected = [
                (friday_ts, fsf, sfs),
                (saturday, sfs, fsf),
                (sunday, fsf, sfs),
            ]

            for day, main, backup in expected:
                current = self.assignment_history.get_record(day)
                if not current or (not current[0] and not current[1]):
                    if apply_additions:
                        self.assignment_history.update_history(day, main, backup)
                        added += 1
                        logger.info(
                            "Sync added assignment for %s: Main=%s, Backup=%s",
                            day.date(),
                            main,
                            backup,
                        )
                elif (current[0] != main) or (current[1] != backup):
                    if overwrite_conflicts:
                        self.assignment_history.update_history(day, main, backup)
                        overwritten += 1
                        logger.info(
                            "Sync overwrote assignment for %s: Main=%s, Backup=%s (was %s/%s)",
                            day.date(),
                            main,
                            backup,
                            current[0],
                            current[1],
                        )
                    else:
                        skipped += 1

        return {
            "added": added,
            "conflicts_overwritten": overwritten,
            "conflicts_skipped": skipped,
        }


def build_scheduler_service(db_name: str = "nurse_schedule.db") -> SchedulerService:
    """Factory for :class:`SchedulerService`.

    Both the GUI (``ui.legacy.App.__init__``) and the CLI menu loop
    (``cli.nurse_scheduler_ui.NurseSchedulerUI``) compose the backend
    through this function so there is exactly one wiring point.
    """
    return SchedulerService(db_name)


def build_scheduler_config_from_settings(settings) -> SchedulerConfig:
    """Translate any settings-like object into a scheduler configuration."""
    return SchedulerConfig(
        weekend_gap_days=settings.get("weekend_gap_days"),
        main_score_factor=settings.get("main_score_factor"),
        backup_score_factor=settings.get("backup_score_factor"),
        availability_penalty=settings.get("availability_penalty"),
        min_days_between_assignments=settings.get("min_days_between_assignments"),
        allow_post_weekend_wednesday_main=settings.get(
            "allow_post_weekend_wednesday_main"
        ),
        allow_post_weekend_wednesday_backup=settings.get(
            "allow_post_weekend_wednesday_backup"
        ),
        allow_post_weekend_thursday_main=settings.get(
            "allow_post_weekend_thursday_main"
        ),
        allow_post_weekend_thursday_backup=settings.get(
            "allow_post_weekend_thursday_backup"
        ),
        allow_one_day_weekday_gap=settings.get("allow_one_day_weekday_gap"),
        scoring_weights=settings.get("scoring_weights"),
    )


def build_scheduler_from_settings(
    start_date,
    end_date,
    nm: NurseManager,
    wh: WeekendHistory,
    ps: PreScheduler,
    settings,
) -> NurseScheduler:
    """Build a scheduler using deterministic nurse ordering and shared settings."""
    config = build_scheduler_config_from_settings(settings)
    non_prn = sorted(nm.get_non_prn_nurses(), key=str.casefold)
    prn = sorted(nm.get_prn_nurses(), key=str.casefold)
    return NurseScheduler(
        start_date,
        end_date,
        non_prn,
        prn,
        nm,
        wh,
        ps,
        config=config,
        history_window_days=settings.get("history_window_days"),
    )


__all__ = [
    "BackendService",
    "SchedulerService",
    "build_scheduler_config_from_settings",
    "build_scheduler_from_settings",
    "build_scheduler_service",
]
