from __future__ import annotations

from typing import Optional


class CandidateDomainBuilder:
    """Build and rank candidate nurse domains for assignment slots."""

    def __init__(self, variant):
        self.variant = variant

    def eligible_domain(
        self,
        date,
        role: str,
        diagnostics: Optional[dict[str, list[str]]] = None,
        *,
        force_relaxed: bool = False,
        gap_mode: bool = False,
    ) -> list[str]:
        variant = self.variant
        diag_map = diagnostics
        created_local_diag = False
        if diag_map is None and variant.assignment_debug_logger.enabled:
            diag_map = {}
            created_local_diag = True
        if diag_map is not None:
            diag_map.clear()

        get_eligible = (
            variant._get_eligible_nurses_for_day_gap
            if gap_mode
            else variant._get_eligible_nurses_for_day
        )
        context = "gap_eligible_domain" if gap_mode else "eligible_domain"

        candidates = get_eligible(
            date,
            role,
            diagnostics=diag_map if diag_map is not None else None,
            relaxed_spacing=False,
        )

        used_relaxed = False
        relaxed_candidates: list[str] = []

        if force_relaxed and variant.config.allow_one_day_weekday_gap:
            capture_relaxed: Optional[dict[str, list[str]]] = {} if diag_map is not None else None
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
        elif not candidates and variant.config.allow_one_day_weekday_gap:
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
                if variant._console_debug and not gap_mode:
                    variant._debug_print(
                        f"[ScheduleVariant] [Domain] relaxed {date.date()} role={role}"
                    )

        if relaxed_candidates:
            seen: set[str] = set(candidates)
            candidates.extend(n for n in relaxed_candidates if n not in seen)

        if not candidates:
            if variant._console_debug and not gap_mode:
                variant._debug_print(
                    f"[ScheduleVariant] [Domain] empty {date.date()} role={role}"
                )
            if variant.assignment_debug_logger.enabled:
                variant._log_assignment_debug(
                    context=context,
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
            variant.state.main_assignment_counts
            if role == "main"
            else variant.state.backup_assignment_counts
        )
        total_counts = variant._get_total_counts()
        order_index = variant._order_index

        wday = int(date.weekday())
        dow_counts = variant._weekday_counts_for(wday) if wday in (0, 1, 2, 3) else {}

        def past_total(nurse: str) -> int:
            return variant.hist_main.get(nurse, 0) + variant.hist_backup.get(nurse, 0)

        candidates.sort(
            key=lambda nurse: (
                role_counts[nurse],
                dow_counts.get(nurse, 0),
                total_counts[nurse],
                past_total(nurse),
                order_index[nurse],
            )
        )

        if variant.assignment_debug_logger.enabled:
            variant._log_assignment_debug(
                context=context,
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
