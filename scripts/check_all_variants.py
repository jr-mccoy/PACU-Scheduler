"""Check whether a weekend variant the cap discarded would have ranked in the top.

Runs a normal generation from a scheduler database (with the saved settings,
including the ``max_weekend_variants`` cap), then checks every weekend
variant without the cap against its top options with
``scheduler.optimization.exhaustive.check_all_weekend_variants``: most
variants are ruled out by bounds, and only the ones that could still rank in
the top are evaluated in full.

Usage::

    python scripts/check_all_variants.py --db nurse_schedule.db \\
        --start 2025-03-01 --end 2025-03-31 [--top 5] [--workers 4]
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
from scheduler.optimization.exhaustive import check_all_weekend_variants  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--workers", type=int, default=None)
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
    began = time.perf_counter()
    run = scheduler.run_generation(max_workers=args.workers, on_progress=lambda d, n: None)
    print(
        f"Normal run: {len(run.candidates)} candidates in {time.perf_counter() - began:.0f} s "
        f"(cap {scheduler.config.max_weekend_variants})"
    )
    if run.status != "ok" or not run.candidates:
        print(f"The run ended {run.status}; nothing to check.")
        return 1

    last = [""]

    def progress(stage, done, total):
        if stage != last[0]:
            print(f"  {stage} ...", flush=True)
            last[0] = stage

    report = check_all_weekend_variants(
        scheduler, run.candidates, top_n=args.top, workers=args.workers, progress=progress
    )
    print(
        f"Checked {report.variants} weekend variants ({report.situations} week situations) "
        f"in {report.seconds:.0f} s:"
    )
    print(f"  {report.ruled_out} provably rank below each of the top {args.top}")
    print(f"  {report.can_only_tie} can at best tie them on every ranking measure")
    print(f"  {report.solved} needed exact minima; {report.evaluated} a full evaluation")
    if report.unchecked:
        print(f"  {report.unchecked} could not be checked (a week had too many fills)")
    if report.better:
        print(f"{len(report.better)} discarded variant(s) would rank in the top {args.top}:")
        for _idx, stats, counts, _schedule in report.better:
            print(
                f"  rank {stats['rank']}: spreads backup {stats['balance_backup']}, "
                f"main {stats['balance_main']}, total {stats['total_spread']}; "
                + ", ".join(f"{n} {c['main']}/{c['backup']}" for n, c in counts.items())
            )
    else:
        print(f"No discarded variant would rank in the top {args.top}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
