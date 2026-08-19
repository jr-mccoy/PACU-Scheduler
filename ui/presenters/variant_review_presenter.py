"""Presenter helpers for variant-review payload and HTML generation."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd


def _build_html_for_top_variants(variants, max_variants: int = 5) -> str:
    """Build a self-contained HTML calendar with tabs for top-ranked variants."""

    cal = calendar.Calendar(firstweekday=6)  # Sunday-first

    def _get_df(var):
        return var[3] if len(var) >= 4 else var[2]

    def _month_sections_for_df(df):
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        day_map = {
            d.date(): (row.get("main", ""), row.get("backup", "")) for d, row in df.iterrows()
        }
        start, end = df.index.min().date(), df.index.max().date()

        months = []
        y, mo = start.year, start.month
        while (y, mo) <= (end.year, end.month):
            months.append((y, mo))
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)

        out = []
        for y, mo in months:
            weeks = cal.monthdayscalendar(y, mo)
            header = f"<div class='month-title'>{calendar.month_name[mo]} {y}</div>"
            table = [
                "<table class='cal'><thead><tr>"
                + "".join(
                    f"<th>{d}</th>" for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                )
                + "</tr></thead><tbody>"
            ]
            for week in weeks:
                tds = []
                for day in week:
                    if day == 0:
                        tds.append("<td class='empty'></td>")
                        continue

                    the_date = datetime(y, mo, day).date()
                    main_name, backup_name = day_map.get(the_date, ("", ""))
                    cell = [f"<div class='date'>{day}</div>"]
                    if main_name:
                        cell.append(
                            f"<div class='role main'><span class='badge'>Main</span> {main_name}</div>"
                        )
                    if backup_name:
                        cell.append(
                            f"<div class='role backup'><span class='badge'>Backup</span> {backup_name}</div>"
                        )
                    tds.append("<td>" + "".join(cell) + "</td>")
                table.append("<tr>" + "".join(tds) + "</tr>")
            table.append("</tbody></table>")
            out.append("<section class='month'>" + header + "".join(table) + "</section>")
        return "".join(out)

    sections = []
    for i, var in enumerate(variants[:max_variants], 1):
        df = _get_df(var)
        sections.append(
            f"<section class='variant' id='v{i}' style='display:none'>"
            f"<h2>Variant {i}</h2>{_month_sections_for_df(df)}</section>"
        )

    css = """
    body { font-family: system-ui, -apple-system, \"Segoe UI\", Roboto, Ubuntu, \"Noto Sans\", Arial, sans-serif; margin: 0; padding: 0 12px 40px; }
    header { position: sticky; top: 0; background: white; padding: 10px 0; border-bottom: 1px solid #ddd; margin-bottom: 10px; }
    .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .controls button { border: 1px solid #bbb; padding: 8px 12px; border-radius: 12px; background: #f9f9f9; cursor: pointer; }
    .controls button.active { background: #e9f3ff; border-color: #6aa1ff; }
    h1 { font-size: 20px; margin: 0 8px 0 0; }
    h2 { font-size: 18px; margin: 8px 0 4px; }
    .month-title { font-weight: 600; margin: 10px 0 4px; }
    table.cal { width: 100%; border-collapse: collapse; table-layout: fixed; margin-bottom: 14px; }
    table.cal th { text-align: center; padding: 6px 0; border-bottom: 1px solid #ccc; font-weight: 600; }
    table.cal td { border: 1px solid #eee; vertical-align: top; height: 110px; padding: 4px; }
    table.cal td .date { font-weight: 600; font-size: 12px; text-align: right; }
    .role { font-size: 12px; margin-top: 2px; }
    .badge { display: inline-block; font-size: 10px; border: 1px solid #aaa; border-radius: 6px; padding: 1px 6px; margin-right: 4px; }
    .role.main .badge { border-color: #2c7be5; }
    .role.backup .badge { border-color: #3bb273; }
    td.empty { background: #fafafa; }
    .month { break-inside: avoid; page-break-inside: avoid; }
    @media print { .controls { display:none } header { position: static } body { padding: 0 } }
    """

    js = """
    function showVariant(i) {
      const vs = document.querySelectorAll('.variant');
      vs.forEach(v => v.style.display = 'none');
      const active = document.getElementById('v'+i);
      if (active) { active.style.display = 'block'; }
      const btns = document.querySelectorAll('.controls button[data-i]');
      btns.forEach(b => b.classList.remove('active'));
      const b = document.querySelector('.controls button[data-i="'+i+'"]');
      if (b) b.classList.add('active');
    }
    window.addEventListener('DOMContentLoaded', () => { showVariant(1); });
    """

    buttons = "".join(
        f"<button data-i='{i}' onclick='showVariant({i})'>Variant {i}</button>"
        for i in range(1, min(len(variants), max_variants) + 1)
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Schedule Variants Calendar</title>
<style>{css}</style>
</head>
<body>
<header>
  <div class="controls">
    <h1>Schedule Variants Calendar</h1>
    {buttons}
    <button onclick="window.print()">Print / Save as PDF</button>
  </div>
</header>
{"".join(sections)}
<script>{js}</script>
</body>
</html>
"""


def _prepare_variant_debug_payload(
    candidate_schedules, scheduler, weekend_history, start_date, history_window: int = 4
):
    """Build a JSON-friendly snapshot describing the ranked schedule variants."""
    try:
        import numpy as _np  # type: ignore
    except Exception:
        _np = None

    def _coerce_timestamp(value):
        if value is None:
            return None
        if isinstance(value, pd.Timestamp):
            return value
        if isinstance(value, datetime):
            return pd.Timestamp(value)
        if isinstance(value, date):
            return pd.Timestamp(value)
        try:
            return pd.Timestamp(value)
        except Exception:
            return None

    def _iso(value):
        if value is None:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time()).isoformat()
        return str(value)

    def _is_empty(val):
        try:
            return pd.isna(val)
        except Exception:
            return val is None

    def _sanitize_number(val):
        if val is None:
            return None
        if _np is not None and isinstance(val, _np.generic):
            return val.item()
        if isinstance(val, (int, float, bool)):
            return val
        try:
            if isinstance(val, timedelta):
                return val.days
        except Exception:
            pass
        try:
            return float(val)
        except Exception:
            return val

    def _sanitize_for_json(value):
        if isinstance(value, dict):
            return {k: _sanitize_for_json(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_sanitize_for_json(v) for v in value]
        if isinstance(value, set):
            return sorted(_sanitize_for_json(v) for v in value)
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time()).isoformat()
        if isinstance(value, timedelta):
            return value.total_seconds()
        if _np is not None and isinstance(value, _np.generic):
            return value.item()
        if isinstance(value, pd.Series):
            return _sanitize_for_json(value.to_dict())
        if isinstance(value, pd.DataFrame):
            return _sanitize_for_json(value.to_dict(orient="records"))
        return value

    start_ts = _coerce_timestamp(start_date)
    variants_payload = []

    viol_counts = None
    if weekend_history and hasattr(weekend_history, "get_violation_counts"):
        try:
            viol_counts = weekend_history.get_violation_counts()
        except Exception:
            viol_counts = None

    overage = None
    if scheduler and hasattr(scheduler, "_historic_overage"):
        try:
            overage = scheduler._historic_overage()
        except Exception:
            overage = None

    for rank, item in enumerate(candidate_schedules, start=1):
        idx = stats = counts = sched_df = None
        try:
            idx, stats, counts, sched_df = item
        except Exception:
            try:
                idx, stats, counts = item[:3]
            except Exception:
                idx = item[0] if item else None
                stats = {}
                counts = {}
            sched_df = item[3] if len(item) > 3 else None

        stats = dict(stats or {})
        counts = dict(counts or {})

        gap_metrics = {
            "gaps": _sanitize_number(stats.get("gaps")),
            "early_gaps": _sanitize_number(stats.get("early_gaps")),
            "rotation_repeats": _sanitize_number(stats.get("rotation_rep")),
            "balance_main": _sanitize_number(stats.get("balance_main")),
            "balance_backup": _sanitize_number(stats.get("balance_backup")),
        }
        if stats.get("balance_main") is not None and stats.get("balance_backup") is not None:
            gap_metrics["balance_total"] = _sanitize_number(
                (stats.get("balance_main") or 0) + (stats.get("balance_backup") or 0)
            )

        if (
            scheduler
            and hasattr(scheduler, "_rotation_violation_score")
            and viol_counts is not None
        ):
            try:
                gap_metrics["rotation_violation_score"] = float(
                    scheduler._rotation_violation_score(counts, viol_counts)
                )
            except Exception:
                pass
        if scheduler and hasattr(scheduler, "_weekend_gap_penalty") and sched_df is not None:
            try:
                gap_metrics["weekend_gap_penalty"] = float(scheduler._weekend_gap_penalty(sched_df))
            except Exception:
                pass
        if scheduler and hasattr(scheduler, "_long_term_score") and overage is not None:
            try:
                gap_metrics["long_term_score"] = float(scheduler._long_term_score(counts, overage))
            except Exception:
                pass

        stats_serializable = {k: _sanitize_number(v) for k, v in stats.items()}
        counts_serializable = {
            nurse: {role: _sanitize_number(val) for role, val in roles.items()}
            for nurse, roles in counts.items()
        }

        weekend_assignments = []
        per_nurse_variant_dates: dict[str, set[pd.Timestamp]] = defaultdict(set)
        if sched_df is not None and not getattr(sched_df, "empty", True):
            df = sched_df.copy()
            weekend_df = df[df["is_weekend"].astype(bool)] if "is_weekend" in df.columns else df

            assignment_rows = []
            for idx_val, row in weekend_df.iterrows():
                ts = _coerce_timestamp(row.get("date", idx_val))
                iso_date = _iso(ts)
                main_val = None if _is_empty(row.get("main")) else str(row.get("main"))
                backup_val = None if _is_empty(row.get("backup")) else str(row.get("backup"))
                assignment_rows.append(
                    (ts, {"date": iso_date, "main": main_val, "backup": backup_val})
                )
                for nurse in (row.get("main"), row.get("backup")):
                    if nurse is None or _is_empty(nurse) or ts is None:
                        continue
                    per_nurse_variant_dates[str(nurse)].add(ts)

            assignment_rows.sort(key=lambda tpl: tpl[0] or pd.Timestamp.min)
            weekend_assignments = [entry for _, entry in assignment_rows]

        spacing_diag = {}
        for nurse in sorted(counts_serializable.keys(), key=str.casefold):
            history_dates = []
            if weekend_history and hasattr(weekend_history, "get_weekends"):
                try:
                    hist = weekend_history.get_weekends(nurse) or []
                except Exception:
                    hist = []
                coerced = [
                    ts
                    for ts in (_coerce_timestamp(h) for h in hist)
                    if ts is not None and (start_ts is None or ts < start_ts)
                ]
                coerced.sort()
                history_dates = coerced[-history_window:] if history_window else coerced

            variant_dates = sorted(per_nurse_variant_dates.get(nurse, set()))
            combined = [(ts, "history") for ts in history_dates] + [
                (ts, "variant") for ts in variant_dates
            ]
            combined.sort(key=lambda tpl: tpl[0])

            combined_entries = []
            prev_ts = None
            for ts, source in combined:
                delta = (
                    int((ts - prev_ts).days) if (prev_ts is not None and ts is not None) else None
                )
                combined_entries.append(
                    {"date": _iso(ts), "source": source, "days_since_prior": delta}
                )
                prev_ts = ts

            spacing_diag[nurse] = {
                "history": [_iso(ts) for ts in history_dates],
                "variant": [_iso(ts) for ts in variant_dates],
                "combined": combined_entries,
            }

        variants_payload.append(
            {
                "rank": rank,
                "variant_index": idx,
                "weighted_score": stats_serializable.get("weighted_score"),
                "stats": stats_serializable,
                "gap_metrics": gap_metrics,
                "assignment_counts": counts_serializable,
                "weekend_assignments": weekend_assignments,
                "spacing_diagnostics": spacing_diag,
            }
        )

    rotation_history = {}
    if scheduler and hasattr(scheduler, "get_rotation_violation_history"):
        try:
            rh = scheduler.get_rotation_violation_history() or {}
        except Exception:
            rh = {}
        rotation_history = {
            nurse: [_iso(_coerce_timestamp(ts)) for ts in (dates or [])]
            for nurse, dates in rh.items()
        }

    scoring_weights = {}
    if scheduler and getattr(scheduler, "config", None) is not None:
        try:
            scoring_weights = dict(getattr(scheduler.config, "scoring_weights", {}) or {})
        except Exception:
            scoring_weights = {}

    payload = {
        "generated_at": datetime.now().isoformat(),
        "start_date": _iso(start_ts),
        "history_window": history_window,
        "scoring_weights": scoring_weights,
        "variants": variants_payload,
        "rotation_violation_history": rotation_history,
    }

    return _sanitize_for_json(payload)
