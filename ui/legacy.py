from __future__ import annotations   # ← add this as the first import      


# ---------------------- Standard Library Imports ----------------------
import sys
import os
import sqlite3
import calendar
from collections import defaultdict
from datetime import date, timedelta, datetime
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import json
import traceback
from contextlib import suppress

# ----------------------- PySide6 Core / Widgets -----------------------
from PySide6.QtCore    import Qt, QDate, QThread, Signal, QSize, QTimer, QObject, QEvent, QStandardPaths, QUrl
from PySide6.QtGui     import (
    QFont, QPalette, QColor, QTextCharFormat, QBrush,
    QCursor, QMovie, QIcon, QPixmap
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QStackedWidget, QLabel, QListWidget,
    QListWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView,
    QCalendarWidget, QProgressDialog, QFormLayout, QLineEdit,
    QCheckBox, QComboBox, QTextEdit, QProgressBar, QButtonGroup,
    QRadioButton, QToolButton, QGridLayout, QFrame, QSizePolicy,
    QAbstractItemView, QStyleOptionViewItem, QStyledItemDelegate,
    QScroller, QScrollerProperties, QScrollArea, QSpinBox, QGroupBox,
    QInputDialog, QDialogButtonBox, QLayout, QGraphicsDropShadowEffect, QTableView,
    QAbstractButton, QStyle
)
from PySide6.QtGui     import QDesktopServices
from typing import Optional, Callable, Any
from PySide6.QtCore import Slot
# ----------------------- Custom Backend Imports -----------------------
from scheduler import (
    NurseManager, PreScheduler, AssignmentHistory, NurseScheduler,
    WeekendHistory, _evaluate_variant_worker, SchedulerConfig, WeekendPattern,
    build_scheduler_service,
    configure_pair_variant_debug, configure_assignment_debug_logger,
)
from .theme import shade_color

# ------------------------------ Constants -----------------------------
DB_NAME = "nurse_schedule.db"
CAL_BORDER = "#E9A9B8"          # soft dark-pink you already use for buttons
DEBUG_SAVE_VARIANTS = bool(int(os.environ.get("NSCHED_DEBUG_VARIANTS", "0")))


# ───────────────────────── Android detection  (must come early) ──────────────────
def is_android_platform() -> bool:
    """Return True when running inside any Android/Python-for-Android build."""
    platform_plugin = os.environ.get("QT_QPA_PLATFORM", "").lower()
    return (
        sys.platform == "android"
        or "ANDROID_ROOT" in os.environ
        or "ANDROID_DATA" in os.environ
        or "ANDROID_STORAGE" in os.environ
        or "ANDROID_ARGUMENT" in os.environ
        or platform_plugin == "android"
        or hasattr(sys, "getandroidapilevel")               # p4a convenience
    )


def _coerce_env_flag(value: Optional[str]) -> Optional[bool]:
    """Map common truthy/falsey strings to bool; return None when unknown."""
    if value is None:
        return None
    val = value.strip().lower()
    if val in {"1", "true", "yes", "on", "t", "threads", "thread"}:
        return True
    if val in {"0", "false", "no", "off", "f", "process", "processes", "proc"}:
        return False
    return None


def _get_setting(settings: Any, key: str, default: Any) -> Any:
    """Safely fetch ``key`` from ``settings`` whether it is a dict or AppSettings."""
    if settings is None:
        return default
    try:
        if hasattr(settings, "get"):
            return settings.get(key)
        if isinstance(settings, dict):
            return settings.get(key, default)
    except Exception:
        return default
    return default


def _normalize_debug_mode(value: Any) -> str:
    """Return a supported NSCHED_DEBUG mode (off/pairs/variants/all)."""
    if not isinstance(value, str):
        return "off"
    mode = value.strip().lower()
    return mode if mode in {"off", "pairs", "variants", "all"} else "off"


def _normalize_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce assorted truthy/falsey representations to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        parsed = _coerce_env_flag(value)
        if parsed is not None:
            return parsed
    return default


def apply_backend_debug_preferences(settings: Any) -> None:
    """
    Configure scheduler backend debug helpers based on persisted settings.

    * ``debug_variant_logging`` controls NSCHED_DEBUG (pairs/variants/all/off)
    * ``assignment_debug_enabled`` toggles the structured AssignmentDebugLogger

    Set ``NSCHED_FORCE_THREAD_POOL=1`` to force ThreadPoolExecutor, 0 to force
    process pools regardless of platform detection (useful for debugging).
    """

    try:
        debug_mode = _normalize_debug_mode(
            _get_setting(settings, "debug_variant_logging", "off")
        )
        assignment_enabled = _normalize_bool(
            _get_setting(settings, "assignment_debug_enabled", True),
            default=True,
        )
    except Exception as exc:  # pragma: no cover - defensive guard for GUI use
        print(f"[debug] failed to read debug settings: {exc}")
        return

    backend_mode = "" if debug_mode == "off" else debug_mode
    if backend_mode:
        os.environ["NSCHED_DEBUG"] = backend_mode
    else:
        os.environ.pop("NSCHED_DEBUG", None)

    try:
        configure_pair_variant_debug(backend_mode)
    except Exception as exc:  # pragma: no cover - errors shouldn't stop GUI
        print(f"[debug] unable to configure NSCHED_DEBUG: {exc}")

    os.environ["DEBUG_SCHED"] = "1" if assignment_enabled else "0"
    try:
        configure_assignment_debug_logger(assignment_enabled)
    except Exception as exc:  # pragma: no cover
        print(f"[debug] unable to configure assignment logger: {exc}")


def tune_dialog(root_layout: QLayout, spacing: int = 12) -> None:
    """
    Recursively apply `spacing` to every QVBox/HBox/Form layout in `root_layout`,
    *except* the private layout Qt creates inside each QDialogButtonBox.
    Works on PySide6 (findChildren must take ONE type).
    """
    for lay in root_layout.findChildren(QLayout, options=Qt.FindChildrenRecursively):
        # skip the invisible layout that lives inside a button-box
        if isinstance(lay.parentWidget(), QDialogButtonBox):
            continue
        if isinstance(lay, (type(root_layout),)):       # quick self-check
            lay.setSpacing(spacing)
        elif lay.__class__.__name__ in ("QVBoxLayout", "QHBoxLayout", "QFormLayout"):
            lay.setSpacing(spacing)
# ---------------------------------------------------------------------------
import re

# ToolDialog now lives in ui.dialogs.tool_dialog;
# imported here so subclasses defined later in this module can extend it.
from .dialogs.tool_dialog import ToolDialog  # noqa: F401

from PySide6.QtWidgets import (
    QTabWidget
)

def _runtime_base_dir() -> str:
    """Directory the script is running from (fallback to CWD)."""
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        if base and os.path.isdir(base):
            return base
    except Exception:
        pass
    return os.getcwd()

# RebuildViolationWorker / ScheduleProgressWorker now live in ui.worker_threads;
# re-imported below for backward compatibility.

def _open_external(path: str) -> bool:
    """
    Try to open a file in the platform default app.
    On Android (Pydroid), fall back to `am start` if QDesktopServices fails.
    Returns True if we *think* the viewer launched.
    """
    try:
        url = QUrl.fromLocalFile(path)
        ok = QDesktopServices.openUrl(url)
        if ok:
            return True
    except Exception:
        pass

    # Fallback for Android: try an intent
    try:
        if is_android_platform():
            import subprocess, mimetypes, shlex
            mt, _ = mimetypes.guess_type(path)
            if not mt:
                # crude guess by extension
                mt = "text/html" if path.lower().endswith(".html") else "application/pdf"
            cmd = f'am start -a android.intent.action.VIEW -d "file://{path}" -t "{mt}"'
            subprocess.run(cmd, shell=True, check=False)
            return True
    except Exception:
        return False
    return False


def _build_html_for_top_variants(variants, max_variants=5) -> str:
    """
    Build a self-contained HTML with tabs for up to `max_variants` variants,
    each shown as month calendars with Main/Backup per day.
    """

    cal = calendar.Calendar(firstweekday=6)  # Sunday-first

    def _get_df(var):
        # variant tuple: (idx, metrics, counts?, df)
        return var[3] if len(var) >= 4 else var[2]

    def _month_sections_for_df(df):
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        day_map = {d.date(): (row.get("main", ""), row.get("backup", ""))
                   for d, row in df.iterrows()}
        start, end = df.index.min().date(), df.index.max().date()
        months = []
        y, mo = start.year, start.month
        while (y, mo) <= (end.year, end.month):
            months.append((y, mo))
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)

        out = []
        for (y, mo) in months:
            weeks = cal.monthdayscalendar(y, mo)
            header = f"<div class='month-title'>{calendar.month_name[mo]} {y}</div>"
            table = ["<table class='cal'><thead><tr>" +
                     "".join(f"<th>{d}</th>" for d in ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]) +
                     "</tr></thead><tbody>"]
            for w in weeks:
                tds = []
                for day in w:
                    if day == 0:
                        tds.append("<td class='empty'></td>")
                    else:
                        the_date = datetime(y, mo, day).date()
                        main_name, backup_name = day_map.get(the_date, ("", ""))
                        cell = [f"<div class='date'>{day}</div>"]
                        if main_name:
                            cell.append(f"<div class='role main'><span class='badge'>Main</span> {main_name}</div>")
                        if backup_name:
                            cell.append(f"<div class='role backup'><span class='badge'>Backup</span> {backup_name}</div>")
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
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, "Noto Sans", Arial, sans-serif; margin: 0; padding: 0 12px 40px; }
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
{''.join(sections)}
<script>{js}</script>
</body>
</html>
"""


def _prepare_variant_debug_payload(candidate_schedules, scheduler, weekend_history, start_date, history_window=4):
    """Build a JSON-friendly snapshot describing the ranked schedule variants."""
    try:
        import numpy as _np  # type: ignore
    except Exception:  # pragma: no cover - numpy may be unavailable in constrained envs
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
            # fall back to positional unpacking with missing dataframe
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

        if scheduler and hasattr(scheduler, "_rotation_violation_score") and viol_counts is not None:
            try:
                gap_metrics["rotation_violation_score"] = float(
                    scheduler._rotation_violation_score(counts, viol_counts)
                )
            except Exception:
                pass
        if scheduler and hasattr(scheduler, "_weekend_gap_penalty") and sched_df is not None:
            try:
                gap_metrics["weekend_gap_penalty"] = float(
                    scheduler._weekend_gap_penalty(sched_df)
                )
            except Exception:
                pass
        if scheduler and hasattr(scheduler, "_long_term_score") and overage is not None:
            try:
                gap_metrics["long_term_score"] = float(
                    scheduler._long_term_score(counts, overage)
                )
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
            if "is_weekend" in df.columns:
                weekend_df = df[df["is_weekend"].astype(bool)]
            else:
                weekend_df = df

            assignment_rows = []
            for idx_val, row in weekend_df.iterrows():
                ts = _coerce_timestamp(row.get("date", idx_val))
                iso_date = _iso(ts)
                main_val = None if _is_empty(row.get("main")) else str(row.get("main"))
                backup_val = None if _is_empty(row.get("backup")) else str(row.get("backup"))
                assignment_rows.append((ts, {
                    "date": iso_date,
                    "main": main_val,
                    "backup": backup_val,
                }))
                for role, nurse in (("main", row.get("main")), ("backup", row.get("backup"))):
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
                    ts for ts in (_coerce_timestamp(h) for h in hist)
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
                delta = None
                if prev_ts is not None and ts is not None:
                    delta = int((ts - prev_ts).days)
                combined_entries.append({
                    "date": _iso(ts),
                    "source": source,
                    "days_since_prior": delta,
                })
                prev_ts = ts

            spacing_diag[nurse] = {
                "history": [_iso(ts) for ts in history_dates],
                "variant": [_iso(ts) for ts in variant_dates],
                "combined": combined_entries,
            }

        variant_entry = {
            "rank": rank,
            "variant_index": idx,
            "weighted_score": stats_serializable.get("weighted_score"),
            "stats": stats_serializable,
            "gap_metrics": gap_metrics,
            "assignment_counts": counts_serializable,
            "weekend_assignments": weekend_assignments,
            "spacing_diagnostics": spacing_diag,
        }
        variants_payload.append(variant_entry)

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


def _write_variant_debug_dump(out_dir, payload, *, owner=None):
    """Persist the prepared payload and clear any temporary scheduler attribute."""
    path = os.path.join(out_dir, "variant_debug.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    if owner and hasattr(owner, "_debug_variant_dump"):
        try:
            delattr(owner, "_debug_variant_dump")
        except Exception:
            pass

    return path


def _save_outputs_for_variants(variants, scheduler, *, top_n=5) -> str:
    # ➊ use script dir instead of Documents
    base_dir = _runtime_base_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(base_dir, f"Schedules_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    # HTML
    try:
        html = _build_html_for_top_variants(variants, max_variants=top_n)
        with open(os.path.join(out_dir, "variants_calendar.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"[export] HTML calendar failed: {e}")

    # PDFs (make sure we don’t mutate the DF used by the dialog)
    try:
        import calendar as _cal
        cal = _cal.Calendar(firstweekday=6)
        for rank, var in enumerate(variants[:top_n], 1):
            df = var[3] if len(var) >= 4 else var[2]
            pdf_path = os.path.join(out_dir, f"variant_{rank}.pdf")
            try:
                scheduler._export_variant_pdf(pdf_path, df.copy(deep=True), cal)  # ➋ pass a copy
            except Exception as ex:
                print(f"[export] PDF for variant {rank} failed: {ex}")
    except Exception as e:
        print(f"[export] PDF batch failed: {e}")

    if DEBUG_SAVE_VARIANTS:
        snapshot = getattr(scheduler, "_debug_variant_dump", None)
        if snapshot is not None:
            try:
                dest = _write_variant_debug_dump(out_dir, snapshot, owner=scheduler)
                print(f"[export] wrote variant debug snapshot to {dest}")
            except Exception as exc:
                print(f"[export] failed to write variant debug snapshot: {exc}")
                try:
                    if hasattr(scheduler, "_debug_variant_dump"):
                        delattr(scheduler, "_debug_variant_dump")
                except Exception:
                    pass

    return out_dir
    
def export_variants_calendar_html(variants, max_variants=5, filename_prefix="schedule_variants"):
    """
    Create the HTML calendar for up to `max_variants` variants, write it to Documents,
    and open it in the default browser. Returns the full file path.
    """
    from datetime import datetime
    html = _build_html_for_top_variants(variants, max_variants=max_variants)

    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.getcwd()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(docs, f"{filename_prefix}_{ts}.html")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    _open_external(out_path)
    return out_path


def export_top_variants_pdfs(variants, scheduler, out_prefix="schedule_variant"):
    """
    Export the given variants as pretty PDFs using the scheduler's ReportLab exporter.
    Writes into Documents and opens the first PDF. Returns list of paths.
    """
    import calendar as _cal
    cal = _cal.Calendar(firstweekday=6)
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.getcwd()

    paths = []
    for rank, var in enumerate(variants, 1):
        df = var[3] if len(var) >= 4 else var[2]
        pdf_path = os.path.join(docs, f"{out_prefix}_{rank}.pdf")
        try:
            scheduler._export_variant_pdf(pdf_path, df, cal)  # uses your existing backend
            paths.append(pdf_path)
        except Exception as e:
            print(f"[pdf export] failed for variant {rank}: {e}")

    if paths:
        _open_external(paths[0])
    return paths    


# Portable screen accessor: works whether you have QGuiApplication or only QApplication
try:
    from PySide6.QtGui import QGuiApplication as _AppScreen
except Exception:
    from PySide6.QtWidgets import QApplication as _AppScreen

class WrappedCheck(QWidget):
    """
    A checkbox that wraps its text to multiple lines on narrow screens.
    Exposes isChecked()/setChecked() and toggled(bool) like QCheckBox.
    """
    toggled = Signal(bool)

    def __init__(self, text: str, checked: bool = False, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._cb = QCheckBox("")
        self._cb.setChecked(checked)
        self._cb.toggled.connect(self.toggled)

        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # Tap label to toggle checkbox
        self._label.mousePressEvent = lambda e: self._cb.toggle()

        lay.addWidget(self._cb, 0, Qt.AlignTop)
        lay.addWidget(self._label, 1)

    def isChecked(self) -> bool:
        return self._cb.isChecked()

    def setChecked(self, v: bool) -> None:
        self._cb.setChecked(v)

    def set_wrap_width(self, w: int) -> None:
        w = max(100, int(w))
        self._label.setFixedWidth(w)


# CompactSettingsDialog and SettingsDialog now live in
# ui.dialogs.compact_settings_dialog and ui.dialogs.settings_dialog;
# re-imported at the bottom of this module for backward compatibility.
# ──────────────────────────────────────────────────────────────────────
#  Additional Helper Functions
# ──────────────────────────────────────────────────────────────────────

def adjust_dialog_for_android(dialog):
    """
    Helper function to adjust any dialog's size for Android screens.
    Call this after creating a dialog but before showing it.
    """
    if sys.platform == 'android' or 'ANDROID_ROOT' in os.environ:
        screen = QApplication.primaryScreen()
        if screen:
            screen_size = screen.size()
            
            # Calculate appropriate size for high-DPI display
            max_width = int(screen_size.width() * 0.92)
            max_height = int(screen_size.height() * 0.85)
            
            # Get current size hint or current size
            current_width = dialog.sizeHint().width() if dialog.sizeHint().isValid() else dialog.width()
            current_height = dialog.sizeHint().height() if dialog.sizeHint().isValid() else dialog.height()
            
            # Constrain to screen limits
            new_width = min(current_width, max_width)
            new_height = min(current_height, max_height)
            
            dialog.resize(new_width, new_height)


def themed_file(basename: str, theme: str) -> str:
    """
    Return  basename_<theme>.ext  if that file exists, otherwise basename.ext.
    Lets you ship arrowL_dark.png / arrowL_light.png next to arrowL.png.
    """
    root, ext = os.path.splitext(basename)
    themed = f"{root}_{theme}{ext}"
    return themed if os.path.exists(themed) else basename


def themed_icon(basename: str, theme: str) -> QIcon:
    """QIcon wrapper for themed_file()."""
    return QIcon(themed_file(basename, theme))

def _shade(hex_rgb: str, k: float) -> str:
    """Deprecated wrapper; use ``ui.theme.shade_color``."""
    return shade_color(hex_rgb, k)


class ConfirmOverlay(QWidget):
    def __init__(self, parent, title, message, yes_cb=None, cancel_cb=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Fill the parent
        self.setGeometry(parent.rect())
        self.setStyleSheet("background:rgba(0,0,0,0.3);")

        card = QWidget(self)
        card.setStyleSheet("""
            background: white; border-radius: 18px;
            padding: 24px;
        """)
        card.setFixedWidth(360)
        card.setFixedHeight(180)
        card.move(
            (self.width() - card.width()) // 2,
            (self.height() - card.height()) // 2
        )

        v = QVBoxLayout(card)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(16)
        lbl_title = QLabel(title)
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("font-size:18pt;font-weight:bold;")
        v.addWidget(lbl_title)

        lbl_msg = QLabel(message)
        lbl_msg.setAlignment(Qt.AlignCenter)
        lbl_msg.setWordWrap(True)
        v.addWidget(lbl_msg)

        h = QHBoxLayout()
        btn_yes = QPushButton("Yes")
        btn_no = QPushButton("Cancel")
        btn_yes.setMinimumWidth(90)
        btn_no.setMinimumWidth(90)
        h.addWidget(btn_yes)
        h.addWidget(btn_no)
        v.addLayout(h)

        btn_yes.clicked.connect(lambda: self._respond(True, yes_cb, cancel_cb))
        btn_no.clicked.connect(lambda: self._respond(False, yes_cb, cancel_cb))

    def _respond(self, accepted, yes_cb, cancel_cb):
        self.close()
        if accepted and yes_cb:
            yes_cb()
        elif not accepted and cancel_cb:
            cancel_cb()
              
# ---------------------------- WrapDelegate ---------------------------
class WrapDelegate(QStyledItemDelegate):
    """Enables word-wrap for QListWidget/QTableView items."""
    def initStyleOption(self, option: QStyleOptionViewItem, index):
        super().initStyleOption(option, index)
        option.features |= QStyleOptionViewItem.WrapText


# PinkHeaderView now lives in ui.widgets.header_views;
# re-imported below for backward compatibility.

# ──────────────────────────────────────────────────────────────
#  Re-usable “make the header pink” patch for *any* QCalendarWidget
# ──────────────────────────────────────────────────────────────

def _apply_header(cal: QCalendarWidget, *, accent="#5C8DBC",
                  fg_white="#FFFFFF", sat_sun="#E53935") -> None:
    """
    Style the built-in header strip using the supplied accent.
    Keeps week-numbers hidden and Sunday/Saturday red.
    """
    accent = shade_color(accent, 1.0)
    fg_white = shade_color(fg_white, 1.0)
    sat_sun = shade_color(sat_sun, 1.0)
    view: QTableView | None = cal.findChild(QTableView)
    if view:
        hh: QHeaderView = view.horizontalHeader()
        pal = hh.palette()
        pal.setColor(QPalette.Base,   QColor(accent))
        pal.setColor(QPalette.Window, QColor(accent))
        pal.setColor(QPalette.Text,   QColor(fg_white))
        hh.setPalette(pal)
        hh.setStyleSheet(
            (
                "QHeaderView::section {"
                f"background:{accent};"
                f"color:{fg_white};"
                "font-weight:600;"
                "font-size:16px;"
                "font-family:Roboto;"
                "border:none;"
                "}"
            )
        )
        for i in range(hh.count()):
            hh.setSectionResizeMode(i, QHeaderView.Stretch)
        vh = view.verticalHeader()
        if vh and vh.isVisible():
            vh.setVisible(False)

    fmt = cal.weekdayTextFormat(Qt.Saturday)
    fmt.setForeground(QColor(sat_sun))
    cal.setWeekdayTextFormat(Qt.Saturday, fmt)
    cal.setWeekdayTextFormat(Qt.Sunday, fmt)
    cal.setGridVisible(True)

# ───────────────────────── calendar-theme wrapper (restored) ──────────────────────
def apply_theme_to_calendar(cal: QCalendarWidget, theme: str, accent: str) -> None:
    """
    Give a vanilla QCalendarWidget a dark / light / pink look and apply the
    accent colour for selections.  Uses the generic _apply_header() helper
    that we kept theme-agnostic.
    """
    if theme == "dark":
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#252A32;  color:#E8EAF0;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#252A32;  color:#E8EAF0;
                gridline-color:#3A404B;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #3A404B;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#3A404B;
            }}
        """)
    elif theme == "light":
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#FFFFFF;  color:#2C2A27;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#FFFFFF;  color:#2C2A27;
                gridline-color:#E1DDD6;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #E1DDD6;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#F5F3F0;
            }}
        """)
    else:      # pink
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#F7D7DF;  color:#4A4A4A;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#F7D7DF;  color:#4A4A4A;
                gridline-color:#E9A9B8;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #E9A9B8;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#F6C3CE;
            }}
        """)

    # header strip (days of week) + red Sat/Sun text
    _apply_header(cal, accent=accent, fg_white="#FFFFFF", sat_sun="#E53935")

# you already have CAL_BORDER = "#E9A9B8" at the top of your file
GRID_COLOR = QColor(CAL_BORDER)

_GRID = QColor("#E9A9B8")        # 1-pixel pink grid



# MultiDatePickerGrid, MultiDatePicker, and SingleDatePicker now live in
# ui.widgets.date_pickers; re-imported at the bottom of this module.
        
from PySide6.QtWidgets import (
    QSpacerItem, QStyleOptionHeader, QStyle
)
import pandas as pd
import time

# MultiLineHeaderView now lives in ui.widgets.header_views;
# re-imported at the bottom of this module for backward compatibility.

from PySide6.QtWidgets import (
    QDialog, QDoubleSpinBox,
)
from PySide6.QtCore import Qt

# RotationViolationDialog now lives in ui.dialogs.rotation_violation_dialog;
# re-imported at the bottom of this module for backward compatibility.
        
# VariantReviewDialog now lives in ui.dialogs.variant_review_dialog;
# re-imported at the bottom of this module for backward compatibility.
        
class UiStyle:
    # ─────────── fonts ───────────
    _BASE_SIZE  = 12
    FONT        = QFont("Roboto", _BASE_SIZE)
    TITLE_FONT  = QFont("Roboto", _BASE_SIZE + 8, QFont.Bold)
    FONT_H1     = TITLE_FONT
    FONT_H2     = QFont("Roboto", _BASE_SIZE + 4, QFont.Medium)
    FONT_BODY   = QFont("Roboto", _BASE_SIZE + 2)

    # ────────── colour helpers ──────────
    @staticmethod
    def _shade(hex_rgb: str, k: float) -> str:
        """Deprecated wrapper; use ``ui.theme.shade_color``."""
        return shade_color(hex_rgb, k)

    # ────────── QSS blocks (shared) ──────────
    _SCROLLBAR_QSS = r"""
        QScrollBar:vertical {{ background:transparent; width:10px; margin:4px 0; }}
        QScrollBar::handle:vertical {{
            background:{ACCENT}; border-radius:5px; min-height:24px;
        }}
        QScrollBar::handle:vertical:hover  {{ background:{ACCENT_DARK}; }}
        QScrollBar::handle:vertical:pressed{{ background:{ACCENT_DARKEST}; }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical     {{ height:0px; }}
    """

    _INPUT_QSS = r"""
        QCheckBox::indicator, QRadioButton::indicator {{ width:24px; height:24px; }}
        QCheckBox::indicator:checked,
        QRadioButton::indicator:checked {{
            background:{ACCENT}; border:1px solid {ACCENT_DARK};
        }}
        QSpinBox::up-button, QSpinBox::down-button {{ width:28px; height:20px; }}
    """

    # ───────────── ENHANCED DARK THEME (Blue-Gray Sophisticated) ─────────────
    _CORE_DARK_QSS = r"""
        QWidget {{ 
            background:#1A1D23; 
            color:#E8EAF0; 
            font-family:Roboto; 
        }}
        QPushButton {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #3A404B, stop:1 #2D3238);
            color:#E8EAF0; 
            border:1px solid #4A5568; 
            border-radius:6px; 
            padding:8px 16px;
            font-weight:500;
        }}
        QPushButton:hover {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #4A5568, stop:1 #3A404B);
            border:1px solid #5A6C7D;
        }}
        QPushButton:pressed {{ 
            background:{ACCENT}; 
            color:#FFFFFF; 
            border:1px solid {ACCENT_DARK};
        }}
        QPushButton[role="special"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 {ACCENT}, stop:1 {ACCENT_DARK}); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QPushButton[role="destructive"] {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #E53935, stop:1 #C62828);
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QListWidget, QTableWidget {{
            background:#252A32; 
            border:1px solid #3A404B;
            selection-background-color:{ACCENT}; 
            selection-color:#FFFFFF;
            alternate-background-color:#2A3038;
        }}
        QHeaderView::section {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #3A404B, stop:1 #2D3238);
            color:#E8EAF0;
            border:1px solid #4A5568;
            padding:8px;
            font-weight:400;
        }}
        QLineEdit, QTextEdit, QComboBox {{
            background:#2D3238; 
            border:1px solid #3A404B; 
            border-radius:4px; 
            padding:3px 4px;
            color:#E8EAF0;
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ 
            border:1px solid {ACCENT}; 
            background:#323842;
        }}
        QTabWidget::pane {{
            border:1px solid #3A404B;
            background:#252A32;
        }}
        QTabBar::tab {{
            background:#2D3238;
            color:#E8EAF0;
            border:1px solid #3A404B;
            padding:4px 8px;
            margin-right:2px;
        }}
        QTabBar::tab:selected {{
            background:{ACCENT};
            color:#FFFFFF;
            border-bottom:none;
        }}
        QGroupBox {{
            color:#E8EAF0;
            border:1px solid #3A404B;
            border-radius:6px;
            margin-top:10px;
            padding-top:10px;
            font-weight:400;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin;
            left:10px;
            padding:0 8px 0 8px;
            background:#1A1D23;
        }}
    """

    # ───────────── ENHANCED LIGHT THEME (Warm Cream Sophisticated) ─────────────
    _CORE_LIGHT_QSS = r"""
        QWidget {{ 
            background:#F8F6F3; 
            color:#2C2A27; 
            font-family:Roboto; 
        }}
        QPushButton {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #FFFFFF, stop:1 #F5F3F0);
            color:#2C2A27; 
            border:1px solid #D4CFC7; 
            border-radius:6px; 
            padding:8px 16px;
            font-weight:500;
        }}
        QPushButton:hover {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #FEFEFE, stop:1 #F0EDE8);
            border:1px solid #C9C3BA;
        }}
        QPushButton:pressed {{ 
            background:{ACCENT}; 
            color:#FFFFFF; 
            border:1px solid {ACCENT_DARK};
        }}
        QPushButton[role="special"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 {ACCENT}, stop:1 {ACCENT_DARK}); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QPushButton[role="destructive"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #E53935, stop:1 #C62828); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QListWidget, QTableWidget {{
            background:#FFFFFF; 
            border:1px solid #E1DDD6;
            selection-background-color:{ACCENT}; 
            selection-color:#FFFFFF;
            alternate-background-color:#FDFCFA;
        }}
        QHeaderView::section {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #F5F3F0, stop:1 #E8E4DE);
            color:#2C2A27;
            border:1px solid #D4CFC7;
            padding:8px;
            font-weight:400;
        }}
        QLineEdit, QTextEdit, QComboBox {{
            background:#FFFFFF; 
            border:1px solid #E1DDD6; 
            border-radius:3px; 
            padding:3px 4px;
            color:#2C2A27;
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ 
            border:2px solid {ACCENT}; 
            background:#FEFEFE;
        }}
        QTabWidget::pane {{
            border:1px solid #E1DDD6;
            background:#FFFFFF;
        }}
        QTabBar::tab {{
            background:#F5F3F0;
            color:#2C2A27;
            border:1px solid #E1DDD6;
            padding:4px 8px;
            margin-right:2px;
        }}
        QTabBar::tab:selected {{
            background:{ACCENT};
            color:#FFFFFF;
            border-bottom:none;
        }}
        QGroupBox {{
            color:#2C2A27;
            border:2px solid #E1DDD6;
            border-radius:4px;
            margin-top:10px;
            padding-top:10px;
            font-weight:400;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin;
            left:10px;
            padding:0 8px 0 8px;
            background:#F8F6F3;
        }}
    """

    # ───────────── PINK THEME (UNCHANGED except selection property) ─────────────
    _CORE_PINK_QSS = r"""
        QWidget {{ background:#FDEDEE; color:#4A4A4A; font-family:Roboto; }}
        QPushButton {{
            background:#F9D1D9; color:#4A4A4A;
            border:1px solid #E9A9B8; border-radius:4px; padding:6px 12px;
        }}
        QPushButton:hover        {{ background:#F6C3CE; }}
        QPushButton:pressed      {{ background:{ACCENT}; color:#FFF; }}
        QPushButton[role="special"] {{ background:#BFA2FF; color:#FFF; border:none; }}
        QPushButton[role="destructive"] {{ background:#FF4F79; color:#FFF; border:none; }}
        QListWidget, QTableWidget {{
            background:#F7D7DF; border:1px solid #E9A9B8;
            selection-background-color:{ACCENT}; selection-color:#FFF;
        }}
        QLineEdit, QTextEdit {{
            background:#FFFFFF; border:1px solid #E9A9B8; border-radius:3px; padding:4px;
        }}
        QLineEdit:focus, QTextEdit:focus {{ border:1px solid {ACCENT}; }}
    """

    @staticmethod
    def apply(app: QApplication, theme="dark", accent_color="#5C8DBC") -> None:
        """Apply palette + QSS to *app* (now supports enhanced dark/light themes)."""
        accent       = accent_color
        accent_dark  = shade_color(accent, 0.85)
        accent_drkst = shade_color(accent, 0.7)

        pal = QPalette()
        if theme == "light":
            # Enhanced warm cream light theme
            pal.setColor(QPalette.Window, QColor("#F8F6F3"))
            pal.setColor(QPalette.WindowText, QColor("#2C2A27"))
            pal.setColor(QPalette.Base, QColor("#FFFFFF"))
            pal.setColor(QPalette.Text, QColor("#2C2A27"))
            pal.setColor(QPalette.Button, QColor("#F5F3F0"))
            pal.setColor(QPalette.ButtonText, QColor("#2C2A27"))
            pal.setColor(QPalette.Highlight, QColor(accent))
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            qss_core = UiStyle._CORE_LIGHT_QSS
        elif theme == "pink":
            # pastel-pink palette (UNCHANGED)
            pal.setColor(QPalette.Window, QColor("#FDEDEE"))
            pal.setColor(QPalette.WindowText, QColor("#4A4A4A"))
            pal.setColor(QPalette.Base, QColor("#FFFFFF"))
            pal.setColor(QPalette.Text, QColor("#4A4A4A"))
            pal.setColor(QPalette.Button, QColor("#F9D1D9"))
            pal.setColor(QPalette.ButtonText, QColor("#4A4A4A"))
            pal.setColor(QPalette.Highlight, QColor("#FF85A1"))   # rose
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            # use rose family for accent overrides
            accent = "#FF85A1"; accent_dark = shade_color(accent, 0.85); accent_drkst = shade_color(accent, 0.7)
            qss_core = UiStyle._CORE_PINK_QSS
        else:   # dark (enhanced blue-gray theme)
            pal.setColor(QPalette.Window, QColor("#1A1D23"))
            pal.setColor(QPalette.WindowText, QColor("#E8EAF0"))
            pal.setColor(QPalette.Base, QColor("#252A32"))
            pal.setColor(QPalette.Text, QColor("#E8EAF0"))
            pal.setColor(QPalette.Button, QColor("#3A404B"))
            pal.setColor(QPalette.ButtonText, QColor("#E8EAF0"))
            pal.setColor(QPalette.Highlight, QColor(accent))
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            qss_core = UiStyle._CORE_DARK_QSS

        app.setPalette(pal)
        app.setStyleSheet(
            (qss_core + UiStyle._SCROLLBAR_QSS + UiStyle._INPUT_QSS).format(
                ACCENT=accent,
                ACCENT_DARK=accent_dark,
                ACCENT_DARKEST=accent_drkst
            )
        )
           

class AppSettings:
    DEFAULTS = {
        # UI
        "theme": "pink",
        "font_size": 12,
        "accent_color": "#5C8DBC",
        "show_gif": True,
        "calendar_grid": True,

        # Core scheduling
        "weekend_gap_days": 28,
        "min_days_between_assignments": 2,
        "main_score_factor": 10,
        "backup_score_factor": 10,
        "availability_penalty": 10,
        "history_window_days": 30,
        "history_duration_months": 6,

        # Analysis / Debug
        "measure_phase_times": True,
        "analyse_initial_weekday_gaps": True,
        "gap_report_file": "weekday_gap_report.txt",
        "debug_variant_logging": "off",
        "assignment_debug_enabled": True,

        # Post-weekend weekday relaxations
        "allow_post_weekend_wednesday_main": False,
        "allow_post_weekend_wednesday_backup": True,
        "allow_post_weekend_thursday_main": True,
        "allow_post_weekend_thursday_backup": True,

        # Hail Mary midweek spacing exception toggles (legacy, still supported)
        "allow_midweek_pair_backup_only": False,
        "allow_midweek_pair_mixed": False,

        # NEW: master toggle for one-day weekday gap fallback
        # (Mon–Wed / Tue–Thu when NOT in a given nurse's pre/post-weekend period)
        "allow_one_day_weekday_gap": False,

        # Canonical scorer weights
        "scoring_weights": {
            "rotation_rep": 0.30,
            "gaps":         0.20,
            "rot_viol":     0.15,
            "weekend_gap":  0.15,
            "balance":      0.10,
            "long_term":    0.10,
        },
    }

    def __init__(self, filename="settings.json"):
        self.path = self._get_config_path(filename)
        self._settings: dict = {}
        self.load()

    def _get_config_path(self, filename):
        base = os.path.expanduser("~")
        cfg_dir = os.path.join(base, ".nurse_scheduler")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, filename)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def _atomic_save(self, data: dict):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def save(self):
        self._atomic_save(self.all())

    def get(self, key):
        return self._settings.get(key, self.DEFAULTS[key])

    def set(self, key, value):
        # Kept for compatibility; writes immediately
        self._settings[key] = value
        self.save()

    def bulk_set(self, mapping: dict):
        """Set many keys and persist ONCE (prevents partial writes)."""
        self._settings.update(mapping or {})
        self.save()

    def all(self) -> dict:
        d = self.DEFAULTS.copy()
        d.update(self._settings)
        return d


def _apply_pink_header(cal: QCalendarWidget, *, accent="#FF4F79",
                       fg_white="#FFFFFF", sat_sun="#E53935") -> None:
    """
    Style the built-in QCalendarWidget: pink/blue header, no week numbers.
    Safe to call multiple times.
    """
    accent = shade_color(accent, 1.0)
    fg_white = shade_color(fg_white, 1.0)
    sat_sun = shade_color(sat_sun, 1.0)
    view: QTableView | None = cal.findChild(QTableView)
    if view:
        # horizontal header (days of week)
        hh: QHeaderView = view.horizontalHeader()
        pal = hh.palette()
        pal.setColor(QPalette.Base,   QColor(accent))
        pal.setColor(QPalette.Window, QColor(accent))
        pal.setColor(QPalette.Text,   QColor(fg_white))
        hh.setPalette(pal)
        hh.setStyleSheet(
            (
                "QHeaderView::section {"
                f"background:{accent};"
                f"color:{fg_white};"
                "font-weight:600;"
                "font-size:16px;"
                "font-family:Roboto;"
                "border:none;"
                "}"
            )
        )
        for i in range(hh.count()):
            hh.setSectionResizeMode(i, QHeaderView.Stretch)

        # *hide* week numbers
        vh = view.verticalHeader()
        if vh and vh.isVisible():
            vh.setVisible(False)

    # Sunday / Saturday text red
    fmt = cal.weekdayTextFormat(Qt.Saturday)
    fmt.setForeground(QColor(sat_sun))
    cal.setWeekdayTextFormat(Qt.Saturday, fmt)
    cal.setWeekdayTextFormat(Qt.Sunday, fmt)

    # make sure grid is on
    cal.setGridVisible(True)


# --- Non-blocking dialogs ---------------------------------------------------

def _resolve_dialog_parent(widget: QWidget | None) -> QWidget | None:
    if widget is None or not isinstance(widget, QWidget):
        return widget

    parent = widget
    while isinstance(parent, QWidget) and not hasattr(parent, "settings"):
        next_parent = parent.parentWidget()
        if next_parent is None:
            break
        parent = next_parent

    if isinstance(parent, QWidget) and hasattr(parent, "settings"):
        return parent

    window = widget.window() if isinstance(widget, QWidget) else None
    if isinstance(window, QWidget) and hasattr(window, "settings"):
        return window

    return widget


def _standard_icon(icon: QStyle.StandardPixmap, size: int = 64) -> QPixmap | None:
    style = QApplication.style()
    if not style:
        return None
    pix = style.standardIcon(icon).pixmap(QSize(size, size))
    return pix if not pix.isNull() else None


def _message_dialog(parent, title: str, message: str, *,
                    icon: QStyle.StandardPixmap | None = None,
                    accent: str | None = None,
                    text_color: str | None = None) -> ToolDialog:
    host = _resolve_dialog_parent(parent)
    dlg = ToolDialog(host, title)
    dlg.setFixedSize(340, 220)
    if accent:
        dlg.refresh_accent(accent)

    layout = QVBoxLayout()
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    if icon is not None:
        icon_lbl = QLabel(alignment=Qt.AlignCenter)
        pix = _standard_icon(icon)
        if pix is not None:
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedHeight(72)
        layout.addWidget(icon_lbl)

    lbl = QLabel(message, alignment=Qt.AlignCenter, wordWrap=True)
    lbl.setFont(QFont("Roboto", 16))
    if text_color:
        lbl.setStyleSheet(f"color:{text_color};")
    layout.addWidget(lbl)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)

    dlg.setLayout(layout)
    dlg.open()
    return dlg


def show_info(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxInformation)


def show_warning(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxWarning,
                           accent="#F5A623", text_color="#8A6D3B")


def show_error(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxCritical,
                           accent="#E53935", text_color="#B71C1C")


def confirm(invoker: QWidget, title: str, message: str,
            yes_cb: Callable[[], None] | None = None,
            cancel_cb: Callable[[], None] | None = None,
            *, yes_text: str = "Yes", cancel_text: str = "Cancel") -> ToolDialog:
    host = _resolve_dialog_parent(invoker)
    dlg = ToolDialog(host, title)
    dlg.setFixedSize(360, 220)

    layout = QVBoxLayout()
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    icon_lbl = QLabel(alignment=Qt.AlignCenter)
    pix = _standard_icon(QStyle.SP_MessageBoxQuestion)
    if pix is not None:
        icon_lbl.setPixmap(pix)
        icon_lbl.setFixedHeight(72)
    layout.addWidget(icon_lbl)

    lbl = QLabel(message, alignment=Qt.AlignCenter, wordWrap=True)
    lbl.setFont(QFont("Roboto", 16))
    layout.addWidget(lbl)

    buttons = QDialogButtonBox()
    yes_btn = buttons.addButton(yes_text, QDialogButtonBox.AcceptRole)
    cancel_btn = buttons.addButton(cancel_text, QDialogButtonBox.RejectRole)
    yes_btn.setDefault(True)
    cancel_btn.setAutoDefault(False)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if yes_cb:
        dlg.accepted.connect(yes_cb)
    if cancel_cb:
        dlg.rejected.connect(cancel_cb)

    dlg.setLayout(layout)
    dlg.open()
    return dlg

# TallHeaderView now lives in ui.widgets.header_views;
# re-imported at the bottom of this module for backward compatibility.


# Screen classes (AdvancedWeekendStatsScreen, MainMenu, NurseManagementScreen,
# PreScheduledScreen, AssignmentHistoryScreen, ViewAllUnavailableScreen,
# ScheduleGenerationScreen, WeekendHistoryCalendarScreen) now live in
# ui.screens.*; re-imported at the bottom of this module for backward
# compatibility.

# ────────────────────────────────────────────────────────────────────
# helper: guarantee high-contrast selections everywhere
def _fix_selection_contrast(widget: QWidget, accent: str) -> None:
    accent = shade_color(accent, 1.0)
    pal = widget.palette()
    pal.setColor(QPalette.Highlight, QColor(accent))
    pal.setColor(QPalette.HighlightedText, Qt.white)
    widget.setPalette(pal)

class App(QMainWindow):
    """Top-level window that hosts the stacked screens."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nurse Scheduler")

        # 1) persistent user settings -------------------------------------------------
        self.settings = AppSettings()

        # 2) apply current style *before* building widgets (important for Android) ---
        UiStyle.apply(
            QApplication.instance(),
            theme=self.settings.get("theme"),          # can be "dark" / "light" / "pink"
            accent_color=self.settings.get("accent_color")
        )

        # 3) stacked container --------------------------------------------------------
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)

        self.backend = build_scheduler_service(DB_NAME)  # SchedulerService composition

        self._pages: list[tuple[str, type[QWidget]]] = [
            ("main",            MainMenu),
            ("manage",          NurseManagementScreen),
            ("prescheduled",    PreScheduledScreen),
            ("assignment_hist", AssignmentHistoryScreen),
            ("view_unavail",    ViewAllUnavailableScreen),
            ("weekend_history", WeekendHistoryCalendarScreen),
            ("generate",        ScheduleGenerationScreen),
            ("advanced_stats",  AdvancedWeekendStatsScreen),  # <-- Add this line
        ]

        # 5) instantiate & register each page -----------------------------------------
        for name, cls in self._pages:
            page = cls(self)
            self.stack.addWidget(page)
            setattr(self, name, page)

        # 6) show main menu
        self.switch_frame("main")

        # 7) final style tweaks (font size, GIF toggle, etc.)
        self.apply_settings()
        apply_backend_debug_preferences(self.settings)

    # ─────────────────────────── navigation helper ───────────────────────────
    def switch_frame(self, name: str):
        """Switch to a named page (see self._pages for valid names)."""
        page_names = [n for n, _ in self._pages]
        if name not in page_names:
            raise ValueError(f"Unknown page: {name}")
        self.stack.setCurrentIndex(page_names.index(name))

    # ─────────────────────────── settings dialog ─────────────────────────────
    def open_settings_dialog(self):
        # Pick compact vs. desktop dialog at runtime
        if sys.platform == "android" or "ANDROID_ROOT" in os.environ:
            dlg = CompactSettingsDialog(self.settings, self)
        else:
            dlg = SettingsDialog(self.settings, self)

        dlg.open()
        dlg.accepted.connect(lambda: self._apply_new_settings(dlg.values()))

    def _apply_new_settings(self, vals: dict) -> None:
        """
        Persist settings atomically and re-apply UI/Theme. Also refresh the
        backend's SharedSettings snapshot so any legacy code reading it gets
        the latest values.
        """
        try:
            self.settings.bulk_set(vals or {})
        except Exception as e:
            print(f"[settings] save failed: {e}")
        self.apply_settings()
        apply_backend_debug_preferences(self.settings)

        # If any code in the backend still reads SharedSettings directly,
        # refresh its snapshot so it sees the newly written settings.json.
        try:
            # SchedulerService instance kept in self.backend
            # It created self.settings = SharedSettings() at init time.
            # Replace it with a fresh snapshot.
            from scheduler import SharedSettings
            self.backend.settings = SharedSettings()
        except Exception:
            pass

    def apply_settings(self) -> None:
        """
        Re-apply font size, palette/QSS, accent colour and live-refresh every
        open widget.  Runs only on the GUI thread (safe for Android).
        """
        app = QApplication.instance()
    
        # ── 1 · pull settings once
        font_size = self.settings.get("font_size")
        theme     = self.settings.get("theme")
        accent    = self.settings.get("accent_color")
        show_gif  = self.settings.get("show_gif")
    
        # ── 2 · global font
        app.setFont(QFont("Roboto", font_size))
    
        # ── 3 · palette + QSS
        UiStyle.apply(app, theme=theme, accent_color=accent)
    
        # helper ensures white-on-accent wherever Qt falls back to palette
        def _fix_selection_contrast(w: QWidget, accent_hex: str) -> None:
            accent_hex = shade_color(accent_hex, 1.0)
            pal = w.palette()
            pal.setColor(QPalette.Highlight,       QColor(accent_hex))
            pal.setColor(QPalette.HighlightedText, Qt.white)
            w.setPalette(pal)
    
        # ── 4 · AUTO SIZE TABLE/LIST VIEWS ───────────────────────────────
        table_views = (
            self.findChildren(QTableWidget) +
            self.findChildren(QTableView)
        )
        list_views  = self.findChildren(QListWidget)
    
        for view in table_views + list_views:
            # keep selections readable
            _fix_selection_contrast(view, accent)
    
            # ----- table-specific tweaks
            if isinstance(view, (QTableWidget, QTableView)):
                fm         = view.fontMetrics()
                row_height = fm.height() + 12                 # 3 px top + 3 px bottom
    
                # vertical header (row numbers / icons)
                vh = view.verticalHeader()
                vh.setMinimumSectionSize(row_height)
                vh.setDefaultSectionSize(row_height)
                vh.setSectionResizeMode(QHeaderView.Fixed)   # consistent everywhere
    
                # horizontal header (column captions)
                hh = view.horizontalHeader()
                view.resizeColumnsToContents()               # natural size first
                hh.setStretchLastSection(True)               # fill remaining space
                hh.setMinimumSectionSize(40)                 # never collapse too far
    
                # nicer look for tall rows on mobile
                hh.setFixedHeight(row_height + 2)
    
                # ── 5 · refresh any open ToolDialogs to new accent/theme
        accent = app.palette().color(QPalette.Highlight).name()   # <<<
        for dlg in self.findChildren(ToolDialog):
            dlg.refresh_accent(accent)        # recolour the border
            if hasattr(dlg, "apply_theme_update"):
                dlg.apply_theme_update()        # ── 6 · calendar pickers (multi + single)
        for picker in self.findChildren(MultiDatePicker):
            picker.set_theme(theme, accent)
        for picker in self.findChildren(SingleDatePicker):
            picker.set_theme(theme, accent)
    
        # ── 7 · main-menu GIF toggle
        if hasattr(self, "main"):
            self.main.update_gif(show_gif)
    
        # ── 8 · screen-level theme hooks
        for name, _cls in self._pages:
            page = getattr(self, name, None)
            if page and hasattr(page, "apply_theme_update"):
                page.apply_theme_update()
                             

# Re-export worker threads from their new home in ui.worker_threads
# (the implementations were moved out of this module in the Phase 6 migration).
from .worker_threads import RebuildViolationWorker, ScheduleProgressWorker  # noqa: F401

# Re-export header view widgets from their new home in ui.widgets.header_views.
from .widgets.header_views import (  # noqa: F401
    MultiLineHeaderView,
    PinkHeaderView,
    TallHeaderView,
)

# Re-export date picker widgets from their new home in ui.widgets.date_pickers.
from .widgets.date_pickers import (  # noqa: F401
    MultiDatePicker,
    MultiDatePickerGrid,
    SingleDatePicker,
)

# Re-export dialogs from their new homes in ui.dialogs.* for backward compatibility.
from .dialogs.compact_settings_dialog import CompactSettingsDialog  # noqa: F401
from .dialogs.settings_dialog import SettingsDialog  # noqa: F401
from .dialogs.rotation_violation_dialog import RotationViolationDialog  # noqa: F401
from .dialogs.variant_review_dialog import VariantReviewDialog  # noqa: F401

# Re-export screens from their new homes in ui.screens.* for backward compatibility.
from .screens.advanced_weekend_stats import AdvancedWeekendStatsScreen  # noqa: F401
from .screens.assignment_history import AssignmentHistoryScreen  # noqa: F401
from .screens.main_menu import MainMenu  # noqa: F401
from .screens.nurse_management import NurseManagementScreen  # noqa: F401
from .screens.prescheduled import PreScheduledScreen  # noqa: F401
from .screens.schedule_generation import ScheduleGenerationScreen  # noqa: F401
from .screens.view_all_unavailable import ViewAllUnavailableScreen  # noqa: F401
from .screens.weekend_history_calendar import WeekendHistoryCalendarScreen  # noqa: F401

# Re-export refactored presenter/service functions for backward compatibility.
from .presenters.variant_review_presenter import (
    _build_html_for_top_variants as _presenter_build_html_for_top_variants,
    _prepare_variant_debug_payload as _presenter_prepare_variant_debug_payload,
)
from .services.variant_export import (
    _save_outputs_for_variants as _service_save_outputs_for_variants,
    export_top_variants_pdfs as _service_export_top_variants_pdfs,
    export_variants_calendar_html as _service_export_variants_calendar_html,
)


def _build_html_for_top_variants(variants, max_variants=5):
    return _presenter_build_html_for_top_variants(variants, max_variants=max_variants)


def _prepare_variant_debug_payload(candidate_schedules, scheduler, weekend_history, start_date, history_window=4):
    return _presenter_prepare_variant_debug_payload(
        candidate_schedules,
        scheduler,
        weekend_history,
        start_date,
        history_window=history_window,
    )


def _save_outputs_for_variants(variants, scheduler, *, top_n=5) -> str:
    return _service_save_outputs_for_variants(
        variants,
        scheduler,
        top_n=top_n,
        debug_save_variants=DEBUG_SAVE_VARIANTS,
    )


def export_variants_calendar_html(variants, max_variants=5, filename_prefix="schedule_variants"):
    return _service_export_variants_calendar_html(
        variants,
        max_variants=max_variants,
        filename_prefix=filename_prefix,
    )


def export_top_variants_pdfs(variants, scheduler, out_prefix="schedule_variant"):
    return _service_export_top_variants_pdfs(variants, scheduler, out_prefix=out_prefix)

# ──────────────────────────────────────────────────────────────────────
#  Entry-point
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    UiStyle.apply(app)           # apply defaults before window creation
    win = App(); win.showMaximized()
    sys.exit(app.exec())
