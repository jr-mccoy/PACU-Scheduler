from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import VariantSearchContext


class CandidateDomainBuilder:
    """Build and rank candidate nurse domains for assignment slots.

    Depends on `VariantSearchContext` instead of `ScheduleVariant` directly,
    so renaming underscored attributes on the variant cannot silently break
    domain construction.
    """

    def __init__(self, context: VariantSearchContext):
        self.context = context

    @property
    def variant(self) -> VariantSearchContext:  # pragma: no cover - shim
        return self.context

    def eligible_domain(
        self,
        date,
        role: str,
        diagnostics: dict[str, list[str]] | None = None,
        *,
        force_relaxed: bool = False,
        gap_mode: bool = False,
    ) -> list[str]:
        ctx = self.context
        diag_map = diagnostics
        created_local_diag = False
        if diag_map is None and ctx.assignment_debug_logger.enabled:
            diag_map = {}
            created_local_diag = True
        if diag_map is not None:
            diag_map.clear()

        get_eligible = (
            ctx.get_eligible_nurses_for_day_gap if gap_mode else ctx.get_eligible_nurses_for_day
        )
        context_label = "gap_eligible_domain" if gap_mode else "eligible_domain"

        candidates = get_eligible(
            date,
            role,
            diagnostics=diag_map if diag_map is not None else None,
            relaxed_spacing=False,
        )

        used_relaxed = False
        relaxed_candidates: list[str] = []

        if force_relaxed and ctx.config.one_day_gap_enabled:
            capture_relaxed: dict[str, list[str]] | None = {} if diag_map is not None else None
            relaxed_candidates = get_eligible(
                date,
                role,
                diagnostics=capture_relaxed,
                relaxed_spacing=True,
            )
            if relaxed_candidates:
                used_relaxed = True
                if diag_map is not None and capture_relaxed is not None:
                    for nurse, reasons in capture_relaxed.items():
                        diag_map.setdefault(nurse, reasons)
        elif not candidates and ctx.config.one_day_gap_enabled:
            if diag_map is not None:
                diag_map.clear()
            candidates = get_eligible(
                date,
                role,
                diagnostics=diag_map if diag_map is not None else None,
                relaxed_spacing=True,
            )
            if candidates:
                used_relaxed = True
                if ctx.console_debug and not gap_mode:
                    ctx.debug_print(f"[ScheduleVariant] [Domain] relaxed {date.date()} role={role}")

        if relaxed_candidates:
            seen: set[str] = set(candidates)
            candidates.extend(n for n in relaxed_candidates if n not in seen)

        if not candidates:
            if ctx.console_debug and not gap_mode:
                ctx.debug_print(f"[ScheduleVariant] [Domain] empty {date.date()} role={role}")
            if ctx.assignment_debug_logger.enabled:
                ctx.log_assignment_debug(
                    context=context_label,
                    phase="candidate_pool",
                    date=date,
                    role=role,
                    eligible=candidates,
                    diagnostics=diag_map or {},
                    final_pick=None,
                    note="no_candidate",
                    extra={
                        "relaxed_spacing": used_relaxed,
                        "eligible_count": 0,
                        "gap_fill": gap_mode,
                        "force_relaxed": force_relaxed,
                    },
                )
            return candidates

        role_counts = (
            ctx.state.main_assignment_counts
            if role == "main"
            else ctx.state.backup_assignment_counts
        )
        total_counts = ctx.get_total_counts()
        order_index = ctx.order_index

        wday = int(date.weekday())
        dow_counts = ctx.weekday_counts_for(wday) if wday in (0, 1, 2, 3) else {}

        def past_total(nurse: str) -> int:
            return ctx.hist_main.get(nurse, 0) + ctx.hist_backup.get(nurse, 0)

        candidates.sort(
            key=lambda nurse: (
                role_counts[nurse],
                dow_counts.get(nurse, 0),
                total_counts[nurse],
                past_total(nurse),
                order_index[nurse],
            )
        )

        if ctx.assignment_debug_logger.enabled:
            ctx.log_assignment_debug(
                context=context_label,
                phase="candidate_pool",
                date=date,
                role=role,
                eligible=candidates,
                diagnostics=diag_map or {},
                final_pick=None,
                note="relaxed_spacing" if used_relaxed else None,
                extra={
                    "relaxed_spacing": used_relaxed,
                    "eligible_count": len(candidates),
                    "gap_fill": gap_mode,
                    "force_relaxed": force_relaxed,
                },
            )

        if created_local_diag:
            diag_map = None
        return candidates
