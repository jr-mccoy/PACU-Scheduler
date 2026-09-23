"""The weekend beam: recent balance, scoring before cloning, and saying when it cut.

Audit finding 14: the beam ranked branches on lifetime history, cloned every
parent × pair before pruning, and nothing told the user the search was capped.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from scheduling_fixtures import build_scheduler, seed_db

import scheduler.engine as engine
from scheduler.domain import ScheduleVariant

START, END = "2026-11-02", "2026-11-29"


def test_a_childs_beam_key_is_known_before_cloning(tmp_path, monkeypatch):
    # C and D are committed to Dec 11, after the window: their child gaps
    # split an existing gap as well as extending one.
    db = seed_db(tmp_path, weekends=[("2026-10-09", "A", "B"), ("2026-12-11", "C", "D")])
    scheduler = build_scheduler(db, START, END)
    friday = pd.Timestamp("2026-11-27")
    weekends = scheduler._get_weekends()
    monkeypatch.setattr(scheduler, "_get_weekends", lambda: weekends[:-1])
    parents = scheduler.generate_all_weekend_variants()[:5]

    checked = 0
    for parent in parents:
        profile = scheduler._beam_profile(parent.state.nurse_weekend_lists)
        for fsf, sfs in [("A", "E"), ("C", "F"), ("G", "H"), ("D", "C")]:
            child = parent.clone()
            child.assign_weekend(friday, fsf, sfs)
            expected = scheduler._variant_beam_key(child)
            assert scheduler._child_beam_key(parent, profile, friday, fsf, sfs) == expected
            checked += 1
    assert checked == 20


def test_a_capped_weekend_clones_only_the_branches_it_keeps(tmp_path, monkeypatch):
    scheduler = build_scheduler(seed_db(tmp_path), START, END, max_weekend_variants=10)
    clones = []
    original = ScheduleVariant.clone

    def counting(self):
        clones.append(1)
        return original(self)

    monkeypatch.setattr(ScheduleVariant, "clone", counting)
    variants = scheduler.generate_all_weekend_variants()

    weekends = len(scheduler._get_weekends())
    assert len(variants) == 10
    assert len(clones) <= 10 * weekends  # not every parent × pair (hundreds each)


def test_old_history_does_not_count_toward_beam_balance(tmp_path):
    ancient = [(f"2024-{month:02d}-05", "A", "B") for month in range(1, 13)]
    db = seed_db(tmp_path, weekends=[(d, a, b) for d, a, b in ancient])
    scheduler = build_scheduler(db, START, END)

    counts, gaps = scheduler._beam_profile(scheduler.get_state_snapshot().nurse_weekend_lists)

    assert counts["A"] == counts["B"] == 0  # years before the window
    assert gaps == []  # none of those gaps ends inside or after the window


def test_the_run_says_when_the_beam_cut_the_search(tmp_path, monkeypatch):
    def fake_worker(item):
        idx, variant, _ = item
        stats = {"rotation_rep": 0, "gaps": 0, "balance_main": 0, "balance_backup": 0}
        counts = {n: {"main": 0, "backup": 0, "total": 0} for n in variant.nurses}
        return idx, stats, counts, variant.state.schedule.copy()

    monkeypatch.setattr(engine, "_evaluate_variant_worker", fake_worker)
    monkeypatch.setattr(engine, "ProcessPoolExecutor", ThreadPoolExecutor)
    db = seed_db(tmp_path)

    capped = build_scheduler(db, START, "2026-11-15", max_weekend_variants=5).run_generation()
    uncapped = build_scheduler(db, START, "2026-11-15", max_weekend_variants=0).run_generation()

    assert capped.search_capped and len(capped.candidates) == 5
    assert not uncapped.search_capped and len(uncapped.candidates) > 5
