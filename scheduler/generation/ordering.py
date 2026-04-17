from __future__ import annotations

import random
import pandas as pd


class OrderGenerator:
    """Generate deterministic variable orders used for full-period refill."""

    def __init__(self, variant):
        self.variant = variant

    def build_full_varlist(self, days, role_order: str = "MB"):
        vars_list = []
        for day in days:
            order = ("main", "backup") if role_order == "MB" else ("backup", "main")
            for role in order:
                if not self.variant._is_pre_scheduled(day, role):
                    val = self.variant.state.schedule.at[day, role]
                    if self.variant.is_empty(val):
                        vars_list.append((day, role))
        return vars_list

    def gen_full_orders(self, days, max_orders: int = 50):
        def week_key(day):
            return day - pd.Timedelta(days=day.weekday())

        orders = []
        chrono = sorted(days)

        orders.append(self.build_full_varlist(chrono, "MB"))
        orders.append(self.build_full_varlist(chrono, "BM"))

        rev = list(reversed(chrono))
        orders.append(self.build_full_varlist(rev, "MB"))
        orders.append(self.build_full_varlist(rev, "BM"))

        dow_blocks = []
        for dow in (0, 1, 2, 3):
            dow_blocks.extend([day for day in chrono if day.weekday() == dow])
        orders.append(self.build_full_varlist(dow_blocks, "MB"))
        orders.append(self.build_full_varlist(dow_blocks, "BM"))

        by_week = {}
        for day in chrono:
            by_week.setdefault(week_key(day), []).append(day)
        week_blocks = []
        for week in sorted(by_week):
            week_blocks.extend(sorted(by_week[week]))
        orders.append(self.build_full_varlist(week_blocks, "MB"))
        orders.append(self.build_full_varlist(week_blocks, "BM"))

        weeks_sorted = [sorted(by_week[week]) for week in sorted(by_week)]
        alt_seq = []
        odds = [weeks_sorted[i] for i in range(0, len(weeks_sorted), 2)]
        evens = [weeks_sorted[i] for i in range(1, len(weeks_sorted), 2)]
        for grp in odds + evens:
            alt_seq.extend(grp)
        orders.append(self.build_full_varlist(alt_seq, "BM"))

        mid_idx = len(chrono) // 2
        middle_out = []
        left, right = mid_idx - 1, mid_idx
        while left >= 0 or right < len(chrono):
            if right < len(chrono):
                middle_out.append(chrono[right])
                right += 1
            if left >= 0:
                middle_out.append(chrono[left])
                left -= 1
        orders.append(self.build_full_varlist(middle_out, "MB"))
        orders.append(self.build_full_varlist(middle_out, "BM"))

        spiral = []
        i, j = 0, len(chrono) - 1
        while i <= j:
            if i <= j:
                spiral.append(chrono[i])
                i += 1
            if i <= j:
                spiral.append(chrono[j])
                j -= 1
        orders.append(self.build_full_varlist(spiral, "BM"))

        def domain_size(day, role):
            dom = self.variant._eligible_domain(day, role)
            return len(dom) if dom else 0

        mrvl = []
        for day in chrono:
            for role in ("main", "backup"):
                if not self.variant._is_pre_scheduled(day, role) and self.variant.is_empty(
                    self.variant.state.schedule.at[day, role]
                ):
                    mrvl.append((day, role))
        mrvl.sort(key=lambda item: domain_size(item[0], item[1]))
        orders.append(mrvl)

        base_vars_mb = self.build_full_varlist(chrono, "MB")
        base_vars_bm = self.build_full_varlist(chrono, "BM")

        mixed_vars = []
        for idx, day in enumerate(chrono):
            if (
                not self.variant._is_pre_scheduled(day, "main")
                and self.variant.is_empty(self.variant.state.schedule.at[day, "main"])
                and not self.variant._is_pre_scheduled(day, "backup")
                and self.variant.is_empty(self.variant.state.schedule.at[day, "backup"])
            ):
                if idx % 2 == 0:
                    mixed_vars.extend([(day, "backup"), (day, "main")])
                else:
                    mixed_vars.extend([(day, "main"), (day, "backup")])
        if mixed_vars:
            orders.append(mixed_vars)

        seeds = [7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 42, 55, 66, 77, 88, 99, 123, 222, 321, 999]
        for seed in seeds:
            if len(orders) >= max_orders:
                break
            rnd_mb = list(base_vars_mb)
            rnd_bm = list(base_vars_bm)
            random.Random(seed).shuffle(rnd_mb)
            random.Random(seed * 3 + 1).shuffle(rnd_bm)
            orders.append(rnd_mb)
            if len(orders) < max_orders:
                orders.append(rnd_bm)

        seen = set()
        unique_orders = []
        for seq in orders[:max_orders]:
            key = tuple(seq)
            if key not in seen:
                seen.add(key)
                unique_orders.append(seq)

        return unique_orders
