from __future__ import annotations

import time


class WindowRefillOptimizer:
    """Orchestrates refill search with centralized tracker lifecycle handling."""

    def __init__(self, variant):
        self.variant = variant

    def _finalize_tracker(self, tracker, initial_quality, improved: bool, label: str) -> bool:
        tracker.restore_global_best()
        final_quality = tracker.get_global_best_quality()
        if final_quality and initial_quality:
            improved = final_quality.is_better_than(initial_quality) or improved
        print(f"[{label}] Final: {initial_quality} -> {final_quality} (improved={bool(improved)})")
        return bool(improved)

    def backtrack_window(
        self,
        vars_list,
        deadline: float,
        node_budget: list[int],
        *,
        gap_mode: bool = False,
        force_relaxed: bool = False,
        depth: int = 0,
    ) -> bool:
        variant = self.variant
        debug_mode = variant._console_debug
        now = time.perf_counter()
        if now >= deadline or node_budget[0] <= 0:
            if debug_mode:
                variant._debug_print(
                    f"[ScheduleVariant] [MRV] cutoff depth={depth} nodes={node_budget[0]} time={now >= deadline}"
                )
            return False

        unassigned = [(d, r) for (d, r) in vars_list if variant.is_empty(variant.state.schedule.at[d, r])]
        if debug_mode:
            variant._debug_print(
                f"[ScheduleVariant] [MRV] enter depth={depth} remaining={len(unassigned)} gap={gap_mode}"
            )
        if not unassigned:
            if debug_mode:
                variant._debug_print(f"[ScheduleVariant] [MRV] success depth={depth}")
            return True

        if gap_mode:
            domain_fn = lambda day, role: variant._eligible_domain_gap(  # noqa: E731
                day, role, force_relaxed=force_relaxed
            )
        else:
            domain_fn = lambda day, role: variant._eligible_domain(  # noqa: E731
                day, role, force_relaxed=force_relaxed
            )

        domains = []
        for day, role in unassigned:
            dom = domain_fn(day, role)
            if not dom:
                if debug_mode:
                    variant._debug_print(
                        f"[ScheduleVariant] [MRV] empty-domain depth={depth} slot={day.date()} role={role}"
                    )
                return False
            domains.append((len(dom), dom, (day, role)))
        domains.sort(key=lambda item: item[0])
        _, first_domain, (day0, role0) = domains[0]

        for nurse in first_domain:
            node_budget[0] -= 1
            now = time.perf_counter()
            if node_budget[0] <= 0 or now >= deadline:
                return False

            if not variant._inc_assign(day0, role0, nurse, gap_phase=gap_mode):
                continue

            failed = False
            fc_radius = max(
                int(variant.config.min_days_between_assignments) + 1,
                variant.DEFAULT_POST_WEEKEND_WINDOW,
                variant.DEFAULT_PRE_WEEKEND_WINDOW,
            )
            for _, _, (dayv, rolev) in domains[1:]:
                if (
                    abs((dayv - day0).days) <= fc_radius
                    and variant.is_empty(variant.state.schedule.at[dayv, rolev])
                    and not domain_fn(dayv, rolev)
                ):
                    failed = True
                    break

            if not failed and self.backtrack_window(
                vars_list,
                deadline,
                node_budget,
                gap_mode=gap_mode,
                force_relaxed=force_relaxed,
                depth=depth + 1,
            ):
                return True

            variant._dec_assign(day0, role0, nurse)

        return False

    def iterative_window_refill_rebalance(
        self,
        window_weeks: int = 3,
        max_passes: int = 6000,
        time_limit_ms: int = 800000,
        node_limit: int = 8000000,
        target_spread=(1, 1),
        tracker=None,
    ) -> bool:
        variant = self.variant

        def good_enough() -> bool:
            if target_spread is None:
                return False
            spread_b, spread_m, _ = variant._spread_components()
            return spread_b <= target_spread[0] and spread_m <= target_spread[1]

        created_tracker = tracker is None
        if created_tracker:
            tracker = variant.BestStateTracker(variant)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.get_global_best_quality()
            if initial_quality is None:
                initial_quality = tracker.initialize()

        windows = variant._collect_weekday_windows(window_weeks=window_weeks)
        if not windows:
            return self._finalize_tracker(tracker, initial_quality, False, "WindowRefill")

        improved = False
        target_hit = good_enough()

        for pass_idx in range(1, max_passes + 1):
            if target_hit:
                break

            if created_tracker:
                tracker.begin_iteration(f"[WindowRefill] Pass {pass_idx}")
            else:
                tracker._iteration_snapshot = variant.StateSnapshot.capture(
                    variant,
                    tracker.get_global_best_quality() or tracker.initialize(),
                )
                tracker._iteration_quality = tracker._iteration_snapshot.quality

            schedule_changed = False
            target_hit_this_pass = False

            for days in windows:
                if not days:
                    continue
                base_tuple = variant._spread_components()
                sub = variant.state.schedule.loc[days, ["main", "backup"]]
                mapper = getattr(sub, "map", None)
                base_mask = mapper(variant.is_empty) if callable(mapper) else sub.applymap(variant.is_empty)
                base_gaps = int(base_mask.to_numpy().sum())

                backup = variant.backup_week_assignments(days)
                variant._clear_window_assignments(days)
                vars_list = variant._build_window_varlist(days)

                deadline = time.perf_counter() + (time_limit_ms / 1000.0)
                node_budget = [node_limit]
                found = self.backtrack_window(vars_list, deadline, node_budget)
                if not found:
                    variant._restore_from_backup(days, backup)
                    continue

                new_tuple = variant._spread_components()
                sub2 = variant.state.schedule.loc[days, ["main", "backup"]]
                mapper2 = getattr(sub2, "map", None)
                new_mask = mapper2(variant.is_empty) if callable(mapper2) else sub2.applymap(variant.is_empty)
                new_gaps = int(new_mask.to_numpy().sum())

                if (variant._lexi_better(new_tuple, base_tuple)) and (new_gaps <= base_gaps):
                    schedule_changed = True
                    if (
                        target_spread is not None
                        and new_tuple[0] <= target_spread[0]
                        and new_tuple[1] <= target_spread[1]
                    ):
                        target_hit_this_pass = True
                        break
                else:
                    variant._restore_from_backup(days, backup)

            comparison = tracker.evaluate_and_commit(
                phase_name=f"[WindowRefill] Pass {pass_idx}",
                allow_neutral=False,
            )
            if comparison == variant.Comparison.BETTER:
                improved = True

            target_hit = target_hit or target_hit_this_pass or good_enough()
            if comparison == variant.Comparison.WORSE and not schedule_changed:
                break

        return self._finalize_tracker(tracker, initial_quality, improved or target_hit, "WindowRefill")

    def iterative_full_period_refill(
        self,
        max_orders: int = 10000,
        per_attempt_time_ms: int = 45000,
        per_attempt_nodes: int = 350000,
        target_spread=(1, 1),
        required_spread: bool = True,
        tracker=None,
    ) -> bool:
        variant = self.variant
        created_tracker = tracker is None
        if created_tracker:
            tracker = variant.BestStateTracker(variant)
            initial_quality = tracker.initialize()
        else:
            initial_quality = tracker.get_global_best_quality()
            if initial_quality is None:
                initial_quality = tracker.initialize()

        spread_b, spread_m, _ = variant._spread_components()
        if spread_b <= target_spread[0] and spread_m <= target_spread[1]:
            return self._finalize_tracker(tracker, initial_quality, True, "FullRefill")

        days = variant._get_all_weekdays()
        if not days:
            return self._finalize_tracker(tracker, initial_quality, False, "FullRefill")

        tracker.begin_iteration("[FullRefill]")
        backup = variant.backup_week_assignments(days)
        base_tuple = variant._spread_components()

        sub = variant.state.schedule.loc[days, ["main", "backup"]]
        mapper = getattr(sub, "map", None)
        base_mask = mapper(variant.is_empty) if callable(mapper) else sub.applymap(variant.is_empty)
        base_gaps = int(base_mask.to_numpy().sum())

        variant._clear_window_assignments(days)
        orders = variant._gen_full_orders(days, max_orders=max_orders)

        improved = False
        best_rows = None
        best_tuple = base_tuple
        success_rows = None

        for order in orders:
            deadline = time.perf_counter() + (per_attempt_time_ms / 1000.0)
            node_budget = [per_attempt_nodes]
            variant._clear_window_assignments(days)

            found = variant._backtrack_full_order(order, deadline, node_budget)
            if not found:
                continue

            new_tuple = variant._spread_components()
            sub2 = variant.state.schedule.loc[days, ["main", "backup"]]
            mapper2 = getattr(sub2, "map", None)
            new_mask = mapper2(variant.is_empty) if callable(mapper2) else sub2.applymap(variant.is_empty)
            new_gaps = int(new_mask.to_numpy().sum())

            if (
                new_tuple[0] <= target_spread[0]
                and new_tuple[1] <= target_spread[1]
                and new_gaps <= base_gaps
            ):
                success_rows = variant.state.schedule.loc[days, ["main", "backup"]].copy()
                improved = True
                break

            if variant._lexi_better(new_tuple, best_tuple) and new_gaps <= base_gaps:
                best_tuple = new_tuple
                best_rows = variant.state.schedule.loc[days, ["main", "backup"]].copy()
                improved = True

        if success_rows is not None:
            variant.state.schedule.loc[days, ["main", "backup"]] = success_rows
            variant._recalculate_assignment_counts()
            variant._update_last_assignment_dates()
        elif not required_spread and improved and best_rows is not None:
            variant.state.schedule.loc[days, ["main", "backup"]] = best_rows
            variant._recalculate_assignment_counts()
            variant._update_last_assignment_dates()
        else:
            variant._restore_from_backup(days, backup)

        comparison = tracker.evaluate_and_commit(phase_name="[FullRefill]", allow_neutral=False)
        if comparison == variant.Comparison.BETTER:
            improved = True

        return self._finalize_tracker(tracker, initial_quality, improved, "FullRefill")
