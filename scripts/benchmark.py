"""Measure how long evaluation takes, and what it produces, per search budget.

Seeds the same fictional roster as ``scripts/demo.py``, builds the weekend
variants, then evaluates each one serially with a chosen budget profile and
prints its time and result quality. Use it to pick ``WorkerTuningConfig``
defaults for a machine: a budget that costs time without improving gaps or
spreads is too generous.

Scenarios:

* ``demo``: the demo roster as seeded.
* ``blocked``: the same, but every regular nurse is off one Wednesday, so two
  slots can never be filled. This is the case that used to run for many
  minutes per variant (audit finding 16).

Usage::

    python scripts/benchmark.py                          # demo, default budgets
    python scripts/benchmark.py --scenario blocked --tuning demo --variants 5
    python scripts/benchmark.py --pool --variants 20     # whole run, worker pool
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import demo  # noqa: E402
import pandas as pd  # noqa: E402

from scheduler import (  # noqa: E402
    WORKER_TUNING,
    NurseManager,
    NurseScheduler,
    PreScheduler,
    WeekendHistory,
)
from scheduler.engine import _evaluate_variant_worker  # noqa: E402

TUNINGS = {"default": WORKER_TUNING, "demo": demo.DEMO_TUNING}


def _scheduler(db: str, start: date, weeks: int, variants: int, tuning) -> NurseScheduler:
    end = start + timedelta(weeks=weeks) - timedelta(days=1)
    nurses = NurseManager(db)
    return NurseScheduler(
        pd.Timestamp(start),
        pd.Timestamp(end),
        nurses.get_non_prn_nurses(),
        nurses.get_prn_nurses(),
        nurses,
        WeekendHistory(db),
        PreScheduler(db),
        config=demo.build_demo_config(variants),
        worker_tuning=tuning,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=("demo", "blocked"), default="demo")
    parser.add_argument("--tuning", choices=sorted(TUNINGS), default="default")
    parser.add_argument("--weeks", type=int, default=4)
    parser.add_argument("--variants", type=int, default=3, help="weekend variants to evaluate")
    parser.add_argument(
        "--pool",
        action="store_true",
        help="time one whole run on the worker pool instead of variants one by one",
    )
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)

    start = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)  # next Monday
    db = str(Path(tempfile.mkdtemp()) / "benchmark.db")
    demo.seed(db, start)
    if args.scenario == "blocked":
        nurses = NurseManager(db)
        wednesday = pd.Timestamp(start + timedelta(days=9))
        for nurse in nurses.get_non_prn_nurses():
            nurses.update_unavailable_dates(
                nurse, set(nurses.get_unavailable_dates(nurse)) | {wednesday}
            )

    tuning = TUNINGS[args.tuning]
    scheduler = _scheduler(db, start, args.weeks, args.variants, tuning)
    print(
        f"{args.scenario} scenario, {args.tuning} budgets, {args.weeks} weeks from {start}, "
        f"up to {args.variants} variants"
    )

    if args.pool:
        began = time.perf_counter()
        best = scheduler.generate_schedule(top_n=1)
        elapsed = time.perf_counter() - began
        if best:
            stats = best[0][1]
            print(
                f"Whole run: {elapsed:.1f} s; best option has {stats['gaps']} unfilled "
                f"({stats.get('unfillable', 0)} impossible), spreads "
                f"{stats['balance_main']}/{stats['balance_backup']}"
            )
        else:
            print(f"Whole run: {elapsed:.1f} s; no feasible schedule")
        return 0

    variants = scheduler.generate_all_weekend_variants()[: args.variants]
    print(f"\n{'Variant':>8}  {'Seconds':>8}  {'Unfilled':>8}  {'Impossible':>10}  Spreads")
    times = []
    for idx, variant in enumerate(variants):
        began = time.perf_counter()
        _, stats, _, _ = _evaluate_variant_worker((idx, variant, tuning))
        times.append(time.perf_counter() - began)
        print(
            f"{idx + 1:>8}  {times[-1]:>8.1f}  {stats['gaps']:>8}  "
            f"{stats.get('unfillable', 0):>10}  {stats['balance_main']}/{stats['balance_backup']}"
        )
    if times:
        print(f"\nMedian {statistics.median(times):.1f} s per variant over {len(times)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
