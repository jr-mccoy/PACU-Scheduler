"""Phase 5 regression tests: VariantSearchContext protocol decoupling.

These tests verify that the search helpers
(`WindowRefillOptimizer`, `CandidateDomainBuilder`, `OrderGenerator`) depend
only on the public `VariantSearchContext` surface, not on `ScheduleVariant`
internals, and that the eager variant→optimizer back-reference is gone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from scheduler import SchedulerConfig, ScheduleState, ScheduleVariant
from scheduler.generation import (
    CandidateDomainBuilder,
    OrderGenerator,
    VariantSearchContext,
)
from scheduler.optimization import WindowRefillOptimizer


class _DummyNurseManager:
    def is_prn_nurse(self, nurse: str) -> bool:
        return False

    def is_late_shift_nurse(self, nurse: str) -> bool:
        return False

    def get_unavailable_dates(self, nurse: str):
        return []


def _build_variant(nurses=("Alice", "Bob")) -> ScheduleVariant:
    idx = pd.date_range("2026-01-05", periods=5, freq="D")  # Mon-Fri
    schedule = pd.DataFrame(index=idx, columns=["main", "backup", "is_weekend"])
    schedule["main"] = None
    schedule["backup"] = None
    schedule["is_weekend"] = [False, False, False, False, False]
    counts_idx = pd.Index(list(nurses))
    state = ScheduleState(
        schedule=schedule,
        main_assignment_counts=pd.Series(0, index=counts_idx),
        backup_assignment_counts=pd.Series(0, index=counts_idx),
        last_assignment={n: None for n in nurses},
        last_pattern={n: None for n in nurses},
        weekend_tracking={},
        nurse_weekend_lists={n: [] for n in nurses},
        rotation_repeats=0,
    )
    availability = pd.DataFrame(True, index=idx, columns=list(nurses))
    return ScheduleVariant(
        state=state,
        nurses=list(nurses),
        availability=availability,
        config=SchedulerConfig(),
        nurse_manager=_DummyNurseManager(),
        pre_scheduled={},
        console_debug=False,
    )


def test_schedule_variant_satisfies_protocol_at_runtime():
    """A live ScheduleVariant must conform to `VariantSearchContext`."""
    variant = _build_variant()
    assert isinstance(variant, VariantSearchContext)


def test_variant_no_longer_eagerly_holds_search_helpers():
    """The back-reference assignments must not run during __init__."""
    variant = _build_variant()
    # The helpers live as cached_property entries — they only appear in
    # __dict__ once first accessed, never eagerly.
    assert "window_optimizer" not in variant.__dict__
    assert "domain_builder" not in variant.__dict__
    assert "order_generator" not in variant.__dict__


def test_cached_property_construction_still_works():
    variant = _build_variant()
    optimizer = variant.window_optimizer
    assert isinstance(optimizer, WindowRefillOptimizer)
    # Second access returns the cached instance.
    assert variant.window_optimizer is optimizer
    # The optimizer holds the variant via its context attribute, not a
    # `.variant` field set in __init__.
    assert optimizer.context is variant


def _make_fake_context():
    """Construct a duck-typed fake context exercising the protocol surface.

    The fake records mutation calls so we can assert the optimizer drives
    them through the public protocol and not via `ScheduleVariant` privates.
    """
    calls: list[tuple] = []

    fake = SimpleNamespace()
    fake.calls = calls

    # Read-only views
    idx = pd.date_range("2026-01-05", periods=2, freq="D")
    fake.state = SimpleNamespace(
        schedule=pd.DataFrame({"main": [None, None], "backup": [None, None]}, index=idx),
        main_assignment_counts=pd.Series([0, 0], index=["Alice", "Bob"]),
        backup_assignment_counts=pd.Series([0, 0], index=["Alice", "Bob"]),
        last_assignment={"Alice": None, "Bob": None},
    )
    fake.config = SimpleNamespace(
        min_days_between_assignments=2,
        allow_one_day_weekday_gap=False,
    )
    fake.nurses = ["Alice", "Bob"]
    fake.pre_scheduled = {}
    fake.hist_main = {}
    fake.hist_backup = {}
    fake.assignment_debug_logger = SimpleNamespace(enabled=False, log=lambda payload: None)
    fake.console_debug = False
    fake.order_index = {"Alice": 0, "Bob": 1}

    # Tracker / snapshot stubs — the optimizer's backtrack path does not
    # need to construct trackers when we only exercise `backtrack_window`.
    fake.BestStateTracker = None
    fake.StateSnapshot = None
    fake.Comparison = SimpleNamespace(BETTER=1, WORSE=-1, EQUAL=0)
    fake.DEFAULT_POST_WEEKEND_WINDOW = 6
    fake.DEFAULT_PRE_WEEKEND_WINDOW = 4

    # Methods
    fake.debug_print = lambda msg, **_: calls.append(("debug_print", msg))
    fake.log_assignment_debug = lambda **kw: calls.append(("log_assignment_debug", kw))
    fake.is_empty = lambda v: v is None or v == ""
    fake.is_pre_scheduled = lambda d, r: False
    fake.is_unfillable = lambda d, r: False

    def eligible_domain(d, r, diagnostics=None, *, force_relaxed=False):
        return ["Alice", "Bob"]

    fake.eligible_domain = eligible_domain
    fake.eligible_domain_gap = eligible_domain
    fake.get_eligible_nurses_for_day = lambda d, r, diagnostics=None, *, relaxed_spacing=False: [
        "Alice",
        "Bob",
    ]
    fake.get_eligible_nurses_for_day_gap = fake.get_eligible_nurses_for_day

    def inc_assign(d, r, n, *, gap_phase=False):
        calls.append(("inc_assign", d, r, n, gap_phase))
        fake.state.schedule.at[d, r] = n
        return True

    def dec_assign(d, r, n):
        calls.append(("dec_assign", d, r, n))
        fake.state.schedule.at[d, r] = None

    fake.inc_assign = inc_assign
    fake.dec_assign = dec_assign
    fake.spread_components = lambda: (0, 0, 0)
    fake.lexi_better = lambda new, base: False
    fake.collect_weekday_windows = lambda window_weeks=2: []
    fake.backup_week_assignments = lambda days: None
    fake.restore_from_backup = lambda days, backup: None
    fake.clear_window_assignments = lambda days: None
    fake.build_window_varlist = lambda days: []
    fake.get_all_weekdays = lambda: list(idx)
    fake.gen_full_orders = lambda days, max_orders=50: []
    fake.backtrack_full_order = lambda vars_list, deadline, node_budget: False
    fake.recalculate_assignment_counts = lambda: None
    fake.update_last_assignment_dates = lambda: None
    fake.get_total_counts = lambda: pd.Series([0, 0], index=["Alice", "Bob"])
    fake.weekday_counts_for = lambda wd: {}

    return fake


def test_window_optimizer_runs_against_fake_context():
    """Proves WindowRefillOptimizer has no implicit ScheduleVariant coupling.

    The fake context implements only the public protocol surface — it is
    *not* a `ScheduleVariant`. If the optimizer ever reaches for a private
    `_xxx` attribute again, this test breaks loudly.
    """
    fake = _make_fake_context()
    optimizer = WindowRefillOptimizer(fake)
    assert optimizer.context is fake

    idx = list(fake.state.schedule.index)
    vars_list = [(idx[0], "main"), (idx[1], "backup")]
    # Generous time budget so the assignment succeeds without timing-out.
    import time

    deadline = time.perf_counter() + 5.0
    node_budget = [1000]
    ok = optimizer.backtrack_window(vars_list, deadline, node_budget)
    assert ok is True
    # Schedule fully filled by the fake context's `inc_assign`.
    for date, role in vars_list:
        assert fake.state.schedule.at[date, role] is not None


def test_candidate_domain_builder_runs_against_fake_context():
    fake = _make_fake_context()
    builder = CandidateDomainBuilder(fake)
    result = builder.eligible_domain(fake.state.schedule.index[0], "main")
    assert result == ["Alice", "Bob"]


def test_order_generator_runs_against_fake_context():
    fake = _make_fake_context()
    generator = OrderGenerator(fake)
    days = list(fake.state.schedule.index)
    vars_list = generator.build_full_varlist(days, "MB")
    assert all(role in ("main", "backup") for _, role in vars_list)
    # No private `_eligible_domain` lookup occurred — only the public hook.
    orders = generator.gen_full_orders(days, max_orders=4)
    assert isinstance(orders, list)


def test_helpers_share_variant_through_protocol_only():
    """The optimizer's only reference to the variant is via `context`."""
    variant = _build_variant()
    optimizer = variant.window_optimizer
    # Public API: optimizer.context returns the variant.
    assert optimizer.context is variant
    # No new private back-reference was introduced (e.g., no ._owner).
    assert not any(name.startswith("_owner") for name in vars(optimizer))


def test_backwards_compatible_variant_alias_still_works():
    """Pre-existing call sites used `optimizer.variant`; keep it as a shim."""
    variant = _build_variant()
    optimizer = variant.window_optimizer
    assert optimizer.variant is variant
    builder = variant.domain_builder
    assert builder.variant is variant
    generator = variant.order_generator
    assert generator.variant is variant


def test_protocol_is_runtime_checkable_for_introspection():
    """`isinstance(obj, VariantSearchContext)` should work without errors."""
    variant = _build_variant()
    assert isinstance(variant, VariantSearchContext)
    # A plain object should not satisfy the protocol.
    assert not isinstance(object(), VariantSearchContext)
