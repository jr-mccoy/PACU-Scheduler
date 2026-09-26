"""Stable public surface that variant search helpers depend on.

`VariantSearchContext` is the protocol that
`scheduler.optimization.WindowRefillOptimizer`,
`scheduler.generation.CandidateDomainBuilder`, and
`scheduler.generation.OrderGenerator` consume. By depending on this
protocol instead of the concrete `ScheduleVariant` class, the search
components no longer reach into `ScheduleVariant` privates — renames of
underscored internals on the variant cannot silently break the helpers.

The protocol is intentionally larger than the minimum recommended in the
phase plan (`assign / unassign / eligible_domain / spread_components /
snapshot / restore`) because the existing helpers were extracted
geographically and still consume the full surface. As underscored
methods are promoted on the variant, they appear here so the helpers
have a stable type to target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VariantSearchContext(Protocol):
    """Public read/write surface used by optimizer/builder/ordering helpers."""

    # ----- Read-only views -----
    state: Any
    config: Any
    nurses: list[str]
    pre_scheduled: Mapping[Any, Mapping[str, str | None]]
    hist_main: Mapping[str, int]
    hist_backup: Mapping[str, int]
    assignment_debug_logger: Any
    console_debug: bool
    order_index: Mapping[str, int]

    # ----- Tracker / snapshot factories -----
    # Concrete classes attached so helpers do not need to import them
    # directly from `scheduler.legacy_core` (avoids a circular import).
    BestStateTracker: type
    StateSnapshot: type
    Comparison: type

    # ----- Search-window tuning constants -----
    DEFAULT_POST_WEEKEND_WINDOW: int
    DEFAULT_PRE_WEEKEND_WINDOW: int

    # ----- Diagnostics -----
    def debug_print(self, msg: str, **kwargs: Any) -> None: ...

    def log_assignment_debug(
        self,
        *,
        context: str,
        phase: str,
        date: Any,
        role: str | None,
        eligible: Iterable[str] | None,
        diagnostics: dict[str, list[str]] | None,
        final_pick: str | None,
        note: str | None = ...,
        extra: dict[str, Any] | None = ...,
    ) -> None: ...

    # ----- Value predicates -----
    def is_empty(self, value: Any) -> bool: ...

    def is_pre_scheduled(self, date: Any, role: str) -> bool: ...

    def is_unfillable(self, date: Any, role: str) -> bool: ...

    # ----- Eligibility / domain queries -----
    def eligible_domain(
        self,
        date: Any,
        role: str,
        diagnostics: dict[str, list[str]] | None = ...,
        *,
        force_relaxed: bool = ...,
    ) -> list[str]: ...

    def eligible_domain_gap(
        self,
        date: Any,
        role: str,
        diagnostics: dict[str, list[str]] | None = ...,
        *,
        force_relaxed: bool = ...,
    ) -> list[str]: ...

    def get_eligible_nurses_for_day(
        self,
        date: Any,
        role: str,
        diagnostics: dict[str, list[str]] | None = ...,
        *,
        relaxed_spacing: bool = ...,
    ) -> list[str]: ...

    def get_eligible_nurses_for_day_gap(
        self,
        date: Any,
        role: str,
        diagnostics: dict[str, list[str]] | None = ...,
        *,
        relaxed_spacing: bool = ...,
    ) -> list[str]: ...

    # ----- Mutation primitives -----
    def inc_assign(
        self,
        date: Any,
        role: str,
        nurse: str,
        *,
        gap_phase: bool = ...,
    ) -> bool: ...

    def dec_assign(self, date: Any, role: str, nurse: str) -> None: ...

    # ----- Spread / quality metrics -----
    def spread_components(self) -> tuple[int, int, int]: ...

    def spreads_at_lower_bound(self) -> bool: ...

    def lexi_better(
        self,
        new_tuple: tuple[int, int, int],
        base_tuple: tuple[int, int, int],
    ) -> bool: ...

    # ----- Window manipulation -----
    def collect_weekday_windows(self, window_weeks: int = ...) -> list[list[Any]]: ...

    def build_window_varlist(self, days: list[Any]) -> list[tuple[Any, str]]: ...

    def clear_window_assignments(self, days: list[Any]) -> None: ...

    def backup_week_assignments(self, days: list[Any]) -> Any: ...

    def restore_from_backup(self, days: list[Any], backup: Any) -> None: ...

    # ----- Full-period helpers -----
    def get_all_weekdays(self) -> list[Any]: ...

    def gen_full_orders(
        self,
        days: list[Any],
        max_orders: int = ...,
    ) -> list[list[tuple[Any, str]]]: ...

    def backtrack_full_order(
        self,
        vars_list: list[tuple[Any, str]],
        deadline: float,
        node_budget: list[int],
    ) -> bool: ...

    # ----- Cache maintenance -----
    def recalculate_assignment_counts(self) -> None: ...

    def get_total_counts(self) -> Any: ...

    def weekday_counts_for(self, weekday: int) -> dict[str, int]: ...


__all__ = ["VariantSearchContext"]
