"""Solve a whole month (weekends and weekdays together) with one CP-SAT model.

Builds the scheduler from a database and the saved settings, solves
``scheduler.optimization.month_model`` for the best months with different
weekends, verifies each through the scheduler's own checks, and prints its
measures and per-nurse counts. ``--csv DIR`` also writes each month as
``month_<n>.csv``. No weekend-variant cap is involved.

Usage::

    python scripts/solve_month.py --db nurse_schedule.db \\
        --start 2025-03-01 --end 2025-03-31 [--top 5] [--csv out/]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scheduler import NurseManager, PreScheduler, SharedSettings, WeekendHistory  # noqa: E402
from scheduler.factory import build_scheduler_from_settings  # noqa: E402
from scheduler.optimization import month_model  # noqa: E402

LABELS = {
    "rotation": "rotation repeats",
    "gaps": "unfilled slots",
    "weekend_gap": "weekend spacing penalty",
    "balance": "main + backup spread",
    "long_term": "fairness penalty",
    "total": "total spread",
    "exceptions": "Tuesday exceptions",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--time-limit", type=float, default=600.0, help="seconds per month")
    parser.add_argument("--csv", type=Path, default=None, help="write each month here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    scheduler = build_scheduler_from_settings(
        pd.Timestamp(args.start),
        pd.Timestamp(args.end),
        NurseManager(args.db),
        WeekendHistory(args.db),
        PreScheduler(args.db),
        SharedSettings(),
    )
    reason = month_model.unsupported(scheduler)
    if reason:
        print(f"The month model cannot be used here: {reason}.")
        return 1

    began = time.perf_counter()
    solutions = month_model.solve_month(
        scheduler, top_n=args.top, workers=args.workers, time_limit_s=args.time_limit
    )
    print(f"{len(solutions)} month(s) in {time.perf_counter() - began:.0f} s")
    if not solutions:
        print("No month satisfies the rules (strict alternation).")
        return 1
    if args.csv:
        args.csv.mkdir(parents=True, exist_ok=True)
    for number, solution in enumerate(solutions, start=1):
        variant, measures, counts = month_model.replay(scheduler, solution)
        verified = "verified" if measures == solution.values else "MEASURES DIFFER"
        proven = "proven best" if solution.optimal else "not proven (time limit)"
        print(f"\nMonth {number} ({proven}; {verified}):")
        print("  " + ", ".join(f"{label} {solution.values[k]}" for k, label in LABELS.items()))
        print(
            "  "
            + "  ".join(f"{n} {c['main']}/{c['backup']}={c['total']}" for n, c in counts.items())
        )
        if args.csv:
            path = args.csv / f"month_{number}.csv"
            variant.state.schedule[["main", "backup"]].to_csv(path)
            print(f"  written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
