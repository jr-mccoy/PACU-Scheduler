"""Generate a schedule from synthetic data, end to end, with no GUI.

The application normally starts against an empty database that a manager
fills in through the GUI, which makes the project awkward to evaluate from
a fresh clone: there is nothing to schedule. This script builds a throwaway
database of fictional staff, seeds enough weekend history for the rotation
rules to have something to alternate against, runs the real generation
pipeline, and prints the winning schedule.

Every name here is invented. No real staffing data is stored in this
repository.

Usage::

    python scripts/demo.py                # 4-week horizon, prints the best schedule
    python scripts/demo.py --weeks 10     # longer horizon
    python scripts/demo.py --keep-db      # leave demo_schedule.db behind to inspect
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scheduler import (  # noqa: E402
    NurseManager,
    NurseScheduler,
    PreScheduler,
    SchedulerConfig,
    SharedSettings,
    WeekendHistory,
    WorkerTuningConfig,
    build_scheduler_config_from_settings,
    ensure_schema,
)
from scheduler.logging_config import configure_logging  # noqa: E402

DEFAULT_DB = "demo_schedule.db"

# (name, is_prn, is_late_shift)
DEMO_NURSES: list[tuple[str, bool, bool]] = [
    ("Avery Brooks", False, False),
    ("Blair Nakamura", False, True),
    ("Casey Odum", False, False),
    ("Devon Ellis", False, True),
    ("Emerson Vance", False, False),
    ("Finley Marsh", False, False),
    ("Harper Quinn", False, True),
    ("Jordan Reyes", False, False),
    ("Kai Whitfield", True, False),
    ("Rowan Sato", True, False),
]

# Weekend history seeded before the horizon. Each entry is
# (weeks_before_start, fsf_nurse, sfs_nurse). The scheduler expects a nurse who
# worked FSF to alternate to SFS next time, so seeding real patterns here is
# what makes the rotation constraint bite during the demo run.
# Kept far enough back that the 28-day minimum weekend gap has cleared by the
# start of the horizon — otherwise every seeded nurse is blocked on day one and
# generation correctly reports the horizon as infeasible.
DEMO_WEEKEND_HISTORY: list[tuple[int, str, str]] = [
    (8, "Avery Brooks", "Blair Nakamura"),
    (7, "Casey Odum", "Devon Ellis"),
    (6, "Emerson Vance", "Finley Marsh"),
    (5, "Harper Quinn", "Jordan Reyes"),
]

# Time-off requests inside the horizon, as (nurse, days_after_start).
DEMO_TIME_OFF: list[tuple[str, int]] = [
    ("Avery Brooks", 3),
    ("Avery Brooks", 4),
    ("Casey Odum", 10),
    ("Devon Ellis", 11),
    ("Devon Ellis", 12),
    ("Harper Quinn", 17),
    ("Jordan Reyes", 24),
]


def _next_friday(from_day: date) -> date:
    """The first Friday on or after ``from_day``."""
    return from_day + timedelta(days=(4 - from_day.weekday()) % 7)


def seed(db_path: str, start: date) -> None:
    """Populate a fresh demo database. Assumes ``db_path`` does not exist."""
    ensure_schema(db_path)

    nurse_manager = NurseManager(db_path)
    for name, is_prn, is_late in DEMO_NURSES:
        nurse_manager.add_nurse(name, is_prn=is_prn, is_late_shift=is_late)

    for name, day_offset in DEMO_TIME_OFF:
        existing = set(nurse_manager.get_unavailable_dates(name))
        existing.add(pd.Timestamp(start + timedelta(days=day_offset)))
        nurse_manager.update_unavailable_dates(name, existing)

    weekend_history = WeekendHistory(db_path)
    for weeks_before, fsf, sfs in DEMO_WEEKEND_HISTORY:
        friday = _next_friday(start) - timedelta(weeks=weeks_before)
        weekend_history.add_assignment(pd.Timestamp(friday), fsf, sfs)


# The shipped WorkerTuningConfig budgets are effectively unbounded — the
# per-attempt time limits alone are 800 seconds, and they are multiplied by
# hundreds of passes — so one variant can take hours. The demo tightens them so
# a run finishes while still exercising the real gap-fill, rebalance, and
# refill passes.
DEMO_TUNING = WorkerTuningConfig(
    gap_fill_iterations=50,
    rebalance_iterations=50,
    window_refill_max_passes=25,
    window_refill_time_limit_ms=2_000,
    window_refill_node_limit=50_000,
    full_period_max_orders=25,
    full_period_per_attempt_time_ms=2_000,
    full_period_per_attempt_nodes=50_000,
)


def build_demo_config(max_variants: int) -> SchedulerConfig:
    """Scheduler settings for the demo, derived from the shipped defaults.

    Only the beam cap is overridden. Weekend variants grow roughly as
    ``(valid pairs) ^ (weekends)`` and every survivor runs the full evaluation
    pipeline, so the shipped default of 500 is far more than a demo needs.
    """
    config = build_scheduler_config_from_settings(SharedSettings())
    config.max_weekend_variants = max_variants
    return config


def generate(
    db_path: str, start: date, end: date, top_n: int, config: SchedulerConfig, workers: int
):
    """Run the real generation pipeline against the demo database."""
    nurse_manager = NurseManager(db_path)
    scheduler = NurseScheduler(
        pd.Timestamp(start),
        pd.Timestamp(end),
        sorted(nurse_manager.get_non_prn_nurses(), key=str.casefold),
        sorted(nurse_manager.get_prn_nurses(), key=str.casefold),
        nurse_manager,
        WeekendHistory(db_path),
        PreScheduler(db_path),
        config=config,
        worker_tuning=DEMO_TUNING,
    )
    return scheduler.generate_schedule(top_n=top_n, max_workers=workers)


def describe(candidates, start: date, end: date) -> None:
    """Print the winning schedule and how the ranked candidates compare."""
    if not candidates:
        print(
            "No schedule satisfied the constraints for this horizon.\n"
            "That is a real outcome, not a crash: strict rotation alternation "
            "can be infeasible. Try --weeks with a longer horizon."
        )
        return

    _idx, _stats, nurse_counts, schedule = candidates[0]

    print(f"\nBest schedule — {start:%b %d, %Y} to {end:%b %d, %Y}")
    print("=" * 62)
    print(schedule[["main", "backup"]].to_string())

    print(f"\nAssignment balance across {len(nurse_counts)} nurses")
    print("-" * 62)
    print(f"{'Nurse':<22}{'Main':>8}{'Backup':>10}{'Total':>8}")
    for nurse in sorted(nurse_counts):
        counts = nurse_counts[nurse]
        main = counts.get("main", 0)
        backup = counts.get("backup", 0)
        print(f"{nurse:<22}{main:>8}{backup:>10}{main + backup:>8}")

    print(f"\n{len(candidates)} candidate schedules survived ranking.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--weeks", type=int, default=4, help="length of the scheduling horizon (default: 4)"
    )
    parser.add_argument(
        "--top-n", type=int, default=5, help="how many ranked candidates to keep (default: 5)"
    )
    parser.add_argument(
        "--max-variants",
        type=int,
        default=8,
        help="beam cap on weekend variants; every survivor is fully evaluated (default: 8)",
    )
    parser.add_argument(
        "--workers", type=int, default=2, help="parallel evaluation workers (default: 2)"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database path (default: {DEFAULT_DB})")
    parser.add_argument(
        "--keep-db", action="store_true", help="do not delete the demo database on exit"
    )
    args = parser.parse_args(argv)

    configure_logging()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()

    start = _next_friday(date.today()) + timedelta(days=3)  # the Monday after
    end = start + timedelta(weeks=args.weeks) - timedelta(days=1)

    try:
        print(f"Seeding {len(DEMO_NURSES)} fictional nurses into {db_path}...")
        seed(str(db_path), start)
        print(f"Generating schedules for {start:%Y-%m-%d} to {end:%Y-%m-%d}...")
        config = build_demo_config(args.max_variants)
        candidates = generate(str(db_path), start, end, args.top_n, config, args.workers)
        describe(candidates, start, end)
    finally:
        if args.keep_db:
            print(f"\nDemo database kept at {db_path}")
        elif db_path.exists():
            db_path.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
