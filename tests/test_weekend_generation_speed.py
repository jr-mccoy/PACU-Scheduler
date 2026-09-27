"""Finding 5 of docs/scheduler-optimization-audit.md: weekend generation.

Within one generation run, a nurse's availability for a weekend, their last
weekend in history, and their next pre-scheduled or recorded weekend depend
only on the nurse and the weekend, so they are cached; and each branch's
schedule is read once per weekend instead of once per nurse check. These
tests compare every variant with what the uncached checks produce.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest
from scheduling_fixtures import build_scheduler, seed_db

from scheduler import NurseScheduler

SCENARIOS = {
    "plain": {},
    "time-off": {
        "time_off": [
            ("A", "2026-11-07"),
            ("B", "2026-11-13"),
            ("C", "2026-11-20"),
            ("D", "2026-11-27"),
        ]
    },
    "history": {
        "weekends": [("2026-10-09", "A", "B"), ("2026-10-16", "C", "D"), ("2026-10-23", "E", "F")]
    },
    "pinned-weekend": {
        "pre_scheduled": [
            ("2026-11-13", "B", "D"),
            ("2026-11-14", "D", "B"),
            ("2026-11-15", "B", "D"),
        ]
    },
    "recorded-after": {"weekends": [("2026-12-04", "A", "C"), ("2026-10-30", "E", "B")]},
    "pinned-after": {"pre_scheduled": [("2026-12-05", "F", None)]},
    # Only Saturday's Backup is pinned: that nurse is already on the
    # weekend being branched on, but not as part of a fixed pair.
    "partial-pin": {"pre_scheduled": [("2026-11-14", None, "D")]},
}
# A window that starts on the Saturday of a weekend already recorded keeps
# that weekend cut: its Friday is outside the schedule.
CUT_START = {"weekends": [("2026-10-30", "A", "B")]}


def _variants(scheduler, *, allow_rotation_violations=False):
    result = scheduler.generate_weekend_candidates(
        allow_rotation_violations=allow_rotation_violations
    )
    return result.status, [
        (
            variant.state.schedule[["main", "backup"]].to_dict("split")["data"],
            sorted(variant.rotation_violations),
        )
        for variant in result.variants
    ]


def _uncached(monkeypatch):
    """Make every run take the original, uncached checks."""
    monkeypatch.setattr(
        NurseScheduler,
        "_weekend_generation_cache",
        property(lambda self: None, lambda self, value: None),
    )


@pytest.mark.parametrize("relaxed", [False, True], ids=["strict", "relaxed"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_cached_generation_matches_the_uncached_checks(tmp_path, monkeypatch, scenario, relaxed):
    db = seed_db(tmp_path, **SCENARIOS[scenario])

    def run(**config):
        scheduler = build_scheduler(db, "2026-11-02", "2026-11-29", **config)
        return _variants(scheduler, allow_rotation_violations=relaxed)

    calls = []
    original = NurseScheduler._check_weekend_gap_from_occupancy

    def spy(self, *args):
        calls.append(1)
        return original(self, *args)

    with monkeypatch.context() as m:
        m.setattr(NurseScheduler, "_check_weekend_gap_from_occupancy", spy)
        fast = [run(), run(max_weekend_variants=7)]
        assert calls, "the cached path should answer the gap checks"
        _uncached(m)
        calls.clear()
        slow = [run(), run(max_weekend_variants=7)]
        assert not calls

    assert fast == slow
    assert fast[0][0] == "ok" and fast[0][1], "the scenario should produce variants"


@pytest.mark.parametrize("relaxed", [False, True], ids=["strict", "relaxed"])
def test_a_cut_weekend_at_the_start_matches_too(tmp_path, monkeypatch, relaxed):
    db = seed_db(tmp_path, **CUT_START)

    def run():
        scheduler = build_scheduler(db, "2026-10-31", "2026-11-29")
        assert scheduler.schedule.index[0] == pd.Timestamp("2026-10-31")
        return _variants(scheduler, allow_rotation_violations=relaxed)

    fast = run()
    with monkeypatch.context() as m:
        _uncached(m)
        assert run() == fast


def test_the_cache_lives_for_one_run_only(tmp_path):
    db = seed_db(tmp_path)
    scheduler = build_scheduler(db, "2026-11-02", "2026-11-15")
    before = len(scheduler.generate_all_weekend_variants())
    assert scheduler._weekend_generation_cache is None

    # Availability changes between runs are seen by the next run.
    for nurse in scheduler.nurses:
        scheduler.availability.loc[pd.Timestamp("2026-11-06"), nurse] = False
    assert scheduler.generate_weekend_candidates().status == "infeasible"
    assert before > 0


@pytest.mark.parametrize("start", ["2026-11-02", "2026-10-31"], ids=["monday", "cut-saturday"])
def test_the_gap_check_matches_on_random_schedules(tmp_path, start):
    """Any schedule, not only ones generation reaches: same answer both ways."""
    db = seed_db(tmp_path, weekends=[("2026-10-30", "A", "B"), ("2026-10-16", "C", "D")])
    scheduler = build_scheduler(db, start, "2026-11-29")
    schedule = scheduler.schedule.copy()
    fridays = [d for d in schedule.index if d.weekday() == 4]
    names = [*scheduler.nurses, None, None, None]
    rng = random.Random(0)
    checked = 0
    for _ in range(40):
        for day in schedule.index:
            schedule.at[day, "main"] = rng.choice(names)
            schedule.at[day, "backup"] = rng.choice(names)
        pre = {
            pd.Timestamp("2026-12-04"): {"fsf": rng.choice(scheduler.nurses)},
            rng.choice(fridays): {"sfs": rng.choice(scheduler.nurses)},
        }
        scheduler._weekend_generation_cache = {}
        occupancy = scheduler._weekend_occupancy(schedule)
        for friday in fridays:
            for nurse in scheduler.nurses:
                slow = scheduler._check_weekend_gap_constraints(nurse, friday, schedule, pre)
                fast = scheduler._check_weekend_gap_constraints(
                    nurse, friday, schedule, pre, occupancy=occupancy
                )
                assert fast == slow, (friday, nurse)
                checked += 1
        scheduler._weekend_generation_cache = None
    assert checked > 1000
