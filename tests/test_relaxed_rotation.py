"""With rotation violations allowed, repeats appear only where needed.

Audit finding 13, as decided: each branch uses strict (alternating) pairs
whenever it has any, and admits a pattern repeat only on a weekend it could
not staff otherwise.
"""

from __future__ import annotations

import pandas as pd
from scheduling_fixtures import build_scheduler, seed_db

ROSTER = [("A", False, False), ("B", False, False), ("C", False, False)]
NOV_6, NOV_13 = pd.Timestamp("2026-11-06"), pd.Timestamp("2026-11-13")


def _generate(db, *, allow):
    scheduler = build_scheduler(
        db, "2026-11-02", "2026-11-15", weekend_gap_days=5, max_weekend_variants=0
    )
    return scheduler.generate_all_weekend_variants(allow_rotation_violations=allow)


def test_relaxed_mode_matches_strict_when_strict_is_feasible(tmp_path):
    db = seed_db(tmp_path, roster=ROSTER, weekends=[("2026-10-02", "A", "B")])

    strict = _generate(db, allow=False)
    relaxed = _generate(db, allow=True)

    assert strict
    assert sorted(str(v.state.weekend_tracking) for v in relaxed) == sorted(
        str(v.state.weekend_tracking) for v in strict
    )
    assert all(v.rotation_violations == [] for v in relaxed)


def test_repeats_appear_only_on_the_weekend_strict_cannot_staff(tmp_path):
    # A and B both last worked FSF, and C (last SFS) is away on Nov 6, so no
    # strict pair exists that weekend. Nov 13 can alternate again.
    db = seed_db(
        tmp_path,
        roster=ROSTER,
        weekends=[("2026-09-04", "A", "C"), ("2026-09-18", "B", "C")],
        time_off=[("C", "2026-11-06"), ("C", "2026-11-07"), ("C", "2026-11-08")],
    )

    assert _generate(db, allow=False) == []
    relaxed = _generate(db, allow=True)

    assert relaxed
    for variant in relaxed:
        assert [friday for friday, _nurse, _pattern in variant.rotation_violations] == [NOV_6]
