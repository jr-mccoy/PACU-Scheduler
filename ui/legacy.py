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
import NCSSQL55 as backend_mod
from NCSSQL55 import (
    NurseManager, PreScheduler, AssignmentHistory, NurseScheduler,
    WeekendHistory, _evaluate_variant_worker, SchedulerConfig, WeekendPattern,
    NurseSchedulerUI
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
    Configure NCSSQL55's debug helpers based on persisted settings.

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
        with suppress(Exception):
            fh = getattr(backend_mod, "_DBG_FILE_PAIRS", None)
            if fh:
                fh.close()
        with suppress(Exception):
            fh = getattr(backend_mod, "_DBG_FILE_VARIANTS", None)
            if fh:
                fh.close()

        backend_mod._DBG_MODE = backend_mode
        backend_mod._DBG_FILE_PAIRS = (
            backend_mod._open_dbg("debug_pairs.txt")
            if backend_mode in {"pairs", "all"}
            else None
        )
        backend_mod._DBG_FILE_VARIANTS = (
            backend_mod._open_dbg("debug_variants.txt")
            if backend_mode in {"variants", "all"}
            else None
        )
    except Exception as exc:  # pragma: no cover - errors shouldn't stop GUI
        print(f"[debug] unable to configure NSCHED_DEBUG: {exc}")

    os.environ["DEBUG_SCHED"] = "1" if assignment_enabled else "0"
    try:
        backend_mod._DEBUG = assignment_enabled
        logger = getattr(backend_mod, "ASSIGNMENT_DEBUG_LOGGER", None)
        if logger:
            with suppress(Exception):
                logger.close()
        backend_mod.ASSIGNMENT_DEBUG_LOGGER = backend_mod.AssignmentDebugLogger(
            enabled=assignment_enabled
        )
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

class ToolDialog(QWidget):
    """
    Non-blocking dialog that shows a darkened scrim and a rounded card.
    The card border colour is the current accent colour taken from the
    parent App (falls back to pink when no settings are available).
    """
    accepted = Signal()
    rejected = Signal()

    def __init__(self, parent=None, title: str | None = None):
        super().__init__(parent)

        self.is_android = is_android_platform()
        if not self.is_android:
            self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint |
                                Qt.CustomizeWindowHint)
            self.setWindowModality(Qt.WindowModal)
        if title:
            self.setWindowTitle(title)

        # ─────────── backdrop scrim ───────────────────────────────────
        self._scrim = QWidget(self, objectName="scrim")
        self._scrim.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._scrim.setStyleSheet("QWidget#scrim { background:rgba(0,0,0,0.18); }")

        # determine accent ------------------------------------------------
        # ToolDialog.__init__
        accent = "#EFA8C0"                             # default fallback
        if parent is not None:                         # ask the palette, not settings
            accent = parent.palette().color(QPalette.Highlight).name()
        # ─────────── card widget ────────────────────────────────────────
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._card = QWidget(self, objectName="card")
        self._card.setStyleSheet(f"""
            QWidget#card {{
                border:4px solid {accent};
                border-radius:16px;
                background:palette(window);
            }}
            QWidget#card QLineEdit,
            QWidget#card QAbstractSpinBox,
            QWidget#card QPlainTextEdit,
            QWidget#card QTextEdit,
            QWidget#card QComboBox {{
                background:palette(base);
            }}
            QWidget#card QCheckBox::indicator {{
                width:20px; height:20px;
                background:palette(base);
                border:1px solid #888; border-radius:3px;
            }}
            QWidget#card QCheckBox::indicator:checked {{
                background:{accent}; border:2px solid {accent};
            }}
        """)
        shadow = QGraphicsDropShadowEffect(blurRadius=24, xOffset=0, yOffset=4,
                                           color=QColor(0, 0, 0, 90))
        self._card.setGraphicsEffect(shadow)

        # ─────────── main layout scaffolding ────────────────────────────
        self._main = QVBoxLayout(self._card)
        self._main.setContentsMargins(24, 24, 24, 24)
        self._main.setSpacing(0)
        self._main_layout = self._main          # <-- legacy alias retained

        self._scroll = QScrollArea(frameShape=QScrollArea.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._scroll.setWidget(self._body)
        self._main.addWidget(self._scroll)

        self._set_default_size()

        # in ToolDialog
    def _apply_card_style(self, accent: str) -> None:
        self._card.setStyleSheet(f"""
            QWidget#card {{
                border:4px solid {accent};
                border-radius:16px;
                background:palette(window);
            }}
            QWidget#card QLineEdit,
            QWidget#card QAbstractSpinBox,
            QWidget#card QPlainTextEdit,
            QWidget#card QTextEdit,
            QWidget#card QComboBox {{
                background:palette(base);
            }}
            QWidget#card QCheckBox::indicator {{
                width:20px; height:20px;
                background:palette(base);
                border:1px solid #888; border-radius:3px;
            }}
            QWidget#card QCheckBox::indicator:checked {{
                background:{accent}; border:2px solid {accent};
            }}
        """)
    
    # in ToolDialog
    def refresh_accent(self, accent: str | None = None) -> None:
        """Re-write the card style with a (new) accent colour."""
        if accent is None:
            accent = QApplication.palette().color(QPalette.Highlight).name()
        self._apply_card_style(accent)    
    
    @staticmethod
    def _fix_selection_contrast(w: QWidget, accent: str) -> None:
        """Force text to stay readable when accenting selection background."""
        accent = shade_color(accent, 1.0)
        pal = w.palette()
        pal.setColor(QPalette.Highlight, QColor(accent))
        pal.setColor(QPalette.HighlightedText, Qt.white)
        w.setPalette(pal)    
        
    # -------------------------------------------------------------------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._scrim.resize(self.size())
        self._card.resize(self.size())

    def setLayout(self, layout):
        """Call AFTER you build the content layout."""
        self._body.setLayout(layout)

    def open(self):
        self.show(); self.raise_(); self.activateWindow()
    def accept(self):
        self.close(); self.accepted.emit()
    def reject(self):
        self.close(); self.rejected.emit()
    def exec(self, *_, **__):
        raise RuntimeError("Use .open() for non-blocking behaviour")

    def _set_default_size(self):
        scr = QApplication.primaryScreen()
        if self.is_android and scr:
            sz = scr.size()
            self.resize(int(sz.width()*0.9), int(sz.height()*0.8))
        else:
            self.resize(640, 480)
            
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

class RebuildViolationWorker(QThread):
    finished = Signal(bool, str)  # (success, message)

    def __init__(self, weekend_history):
        super().__init__()
        self.weekend_history = weekend_history

    def run(self):
        try:
            self.weekend_history._recalculate_violation_counts()
            self.finished.emit(True, "Violation history rebuilt.")
        except Exception as e:
            self.finished.emit(False, f"Error: {e}")

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


class CompactSettingsDialog(ToolDialog):
    """
    Phone-friendly settings dialog:
      - Vertical scroll only (no horizontal scroll)
      - Buttons pinned and always visible
      - Form rows wrap on narrow screens
      - Pages do not over-expand vertically
    """

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent, title="Settings")
        self.settings = settings
    
        # Build the scroll-body content (goes inside ToolDialog's scroll area)
        body = QVBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(10)
    
        # ---- Tabs --------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        bar = self.tabs.tabBar()
        bar.setExpanding(True)
        bar.setElideMode(Qt.ElideRight)
        body.addWidget(self.tabs)
    
        # Helper to build narrow-friendly FormLayouts
        def make_form(parent=None):
            form = QFormLayout(parent)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)  # label wraps above field if too narrow
            form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(10)
            form.setContentsMargins(6, 6, 6, 6)
            return form
    
        # ---------------- UI TAB ----------------
        ui_tab = QWidget()
        ui_form = make_form(ui_tab)
    
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light", "pink"])
        self.theme_combo.setCurrentText(settings.get("theme"))
        self.theme_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ui_form.addRow("Theme:", self.theme_combo)
    
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 32)
        self.font_spin.setValue(settings.get("font_size"))
        self.font_spin.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.font_spin.setMinimumWidth(0)
        ui_form.addRow("Font size:", self.font_spin)
    
        # Accent row (editor + swatch in a compact container)
        self.accent_edit = QLineEdit(settings.get("accent_color"))
        self.accent_edit.setMinimumWidth(0)
        self.accent_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    
        self.accent_swatch = QLabel()
        self.accent_swatch.setFixedSize(28, 28)
        self.accent_swatch.setStyleSheet(f"""
            background: {settings.get('accent_color')};
            border: 2px solid #888; border-radius: 6px;
        """)
    
        acc_row = QWidget()
        acc_lay = QHBoxLayout(acc_row)
        acc_lay.setContentsMargins(0, 0, 0, 0)
        acc_lay.setSpacing(8)
        acc_lay.addWidget(self.accent_edit, 1)
        acc_lay.addWidget(self.accent_swatch, 0)
        ui_form.addRow("Accent color:", acc_row)
    
        def update_swatch():
            color = self.accent_edit.text()
            self.accent_swatch.setStyleSheet(f"""
                background: {color if color.startswith('#') and len(color) == 7 else '#5C8DBC'};
                border: 2px solid #888; border-radius: 6px;
            """)
        self.accent_edit.textChanged.connect(update_swatch)
    
        self.gif_chk = QCheckBox("Show animated GIF")
        self.gif_chk.setChecked(settings.get("show_gif"))
        ui_form.addRow("", self.gif_chk)
    
        self.grid_chk = QCheckBox("Show calendar grid")
        self.grid_chk.setChecked(settings.get("calendar_grid"))
        ui_form.addRow("", self.grid_chk)
    
        self.tabs.addTab(ui_tab, "UI")
    
        # ---------------- SCHEDULE TAB ----------------
        sched_tab = QWidget()
        sched_form = make_form(sched_tab)
    
        def sb(val, lo, hi):
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setValue(val)
            box.setMinimumHeight(40)
            box.setMinimumWidth(0)
            box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            return box
    
        self.weekend_gap   = sb(settings.get("weekend_gap_days"), 7, 90)
        self.min_between   = sb(settings.get("min_days_between_assignments"), 0, 7)
        self.main_factor   = sb(settings.get("main_score_factor"), 1, 100)
        self.backup_factor = sb(settings.get("backup_score_factor"), 1, 100)
        self.avail_penalty = sb(settings.get("availability_penalty"), 0, 100)
        self.hist_window   = sb(settings.get("history_window_days"), 1, 365)
        self.hist_duration = sb(settings.get("history_duration_months"), 1, 60)
    
        sched_form.addRow("Weekend gap (days):", self.weekend_gap)
        sched_form.addRow("Min days between:", self.min_between)
        sched_form.addRow("Main score factor:", self.main_factor)
        sched_form.addRow("Backup score factor:", self.backup_factor)
        sched_form.addRow("Availability penalty:", self.avail_penalty)
        sched_form.addRow("History window (days):", self.hist_window)
        sched_form.addRow("History duration (mo):", self.hist_duration)
    
        self.tabs.addTab(sched_tab, "Schedule")
    
        # ---------------- DEBUG TAB ----------------
        debug_tab = QWidget()
        dbg_outer = QVBoxLayout(debug_tab)
        dbg_outer.setContentsMargins(6, 6, 6, 6)
        dbg_outer.setSpacing(8)
    
        self.measure_chk = QCheckBox("Measure phase times")
        self.measure_chk.setChecked(settings.get("measure_phase_times"))
        self.analyse_chk = QCheckBox("Analyse initial gaps")
        self.analyse_chk.setChecked(settings.get("analyse_initial_weekday_gaps"))
        self.assignment_debug_chk = QCheckBox("Enable structured assignment debug logging")
        self.assignment_debug_chk.setChecked(settings.get("assignment_debug_enabled"))
        dbg_outer.addWidget(self.measure_chk)
        dbg_outer.addWidget(self.analyse_chk)
        dbg_outer.addWidget(self.assignment_debug_chk)

        dbg_form = make_form()
        self.debug_mode_combo = QComboBox()
        self.debug_mode_combo.addItem("Off", "off")
        self.debug_mode_combo.addItem("Pairs only", "pairs")
        self.debug_mode_combo.addItem("Variants only", "variants")
        self.debug_mode_combo.addItem("Pairs + variants", "all")
        current_mode = settings.get("debug_variant_logging")
        idx = self.debug_mode_combo.findData(current_mode)
        if idx < 0:
            idx = 0
        self.debug_mode_combo.setCurrentIndex(idx)
        dbg_form.addRow("Variant debug:", self.debug_mode_combo)
        self.gap_file = QLineEdit(settings.get("gap_report_file"))
        self.gap_file.setMinimumWidth(0)
        self.gap_file.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dbg_form.addRow("Gap report file:", self.gap_file)
        dbg_outer.addLayout(dbg_form)
        dbg_outer.addStretch()
    
        self.tabs.addTab(debug_tab, "Debug")
    
        # ---------------- RELAXATION TAB ----------------
        relax_tab = QWidget()
        relax_form = make_form(relax_tab)
    
        self.wed_main   = QCheckBox("Allow MAIN on Wednesday")
        self.wed_main.setChecked(settings.get("allow_post_weekend_wednesday_main"))
        self.wed_backup = QCheckBox("Allow BACKUP on Wednesday")
        self.wed_backup.setChecked(settings.get("allow_post_weekend_wednesday_backup"))
        self.thu_main   = QCheckBox("Allow MAIN on Thursday")
        self.thu_main.setChecked(settings.get("allow_post_weekend_thursday_main"))
        self.thu_backup = QCheckBox("Allow BACKUP on Thursday")
        self.thu_backup.setChecked(settings.get("allow_post_weekend_thursday_backup"))
    
        # Wrap the long ones so they flow to 2nd line on phones
        self.hm_backup = WrappedCheck(
            "Allow Mon–Wed / Tue–Thu one-day gap (both BACKUP)",
            checked=settings.get("allow_midweek_pair_backup_only")
        )
        self.hm_mixed = WrappedCheck(
            "Allow Mon–Wed / Tue–Thu one-day gap (MAIN + BACKUP)",
            checked=settings.get("allow_midweek_pair_mixed")
        )
    
        for cb in (self.wed_main, self.wed_backup, self.thu_main, self.thu_backup):
            relax_form.addRow("", cb)
        relax_form.addRow("", self.hm_backup)
        relax_form.addRow("", self.hm_mixed)
    
        # --- NEW: master toggle for one-day weekday gap fallback
        self.one_day_gap = WrappedCheck(
            "Enable one-day weekday gap fallback (Mon–Wed / Tue–Thu) outside pre/post-weekend",
            checked=settings.get("allow_one_day_weekday_gap")
        )
        relax_form.addRow("", self.one_day_gap)
    
        self.tabs.addTab(relax_tab, "Relaxation")
    
        # Install the scroll body into ToolDialog (inside its scroll area)
        self.setLayout(body)
    
        # ---- Buttons pinned BELOW the scroll area (always visible) -------
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        if getattr(self, "is_android", False):
            for b in btns.buttons():
                b.setMinimumHeight(48)
        self._main_layout.addWidget(btns)
    
        # Ensure no horizontal scrollbars and keep content width in sync
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.viewport().installEventFilter(self)
    
        self.resize(560, 680)
    
    def showEvent(self, e):
        super().showEvent(e)
        scr = (self.windowHandle().screen()
               if self.windowHandle() and self.windowHandle().screen()
               else _AppScreen.primaryScreen())
        if scr:
            geo = scr.availableGeometry()
            max_h = int(geo.height() * 0.85)
            max_w = int(geo.width() * 0.95)
            self.setMaximumSize(max_w, max_h)
            self.resize(min(self.width(), max_w), min(self.height(), max_h))
        QTimer.singleShot(0, self._sync_body_width)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._sync_body_width)

    def eventFilter(self, obj, ev):
        if obj is self._scroll.viewport() and ev.type() == QEvent.Resize:
            self._sync_body_width()
        return super().eventFilter(obj, ev)

    def _sync_body_width(self):
        """Force the scroll-body, tabs and wrapped check texts to match viewport width."""
        if not self._scroll or not self._body:
            return
        vw = self._scroll.viewport().width()
        if vw <= 0:
            return
        self._body.setMinimumWidth(vw)
        self._body.setMaximumWidth(vw)
        self.tabs.setMaximumWidth(vw)
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            page.setMinimumWidth(0)
            page.setMaximumWidth(vw)
        # Make sure the long Relaxation labels wrap within the field column
        wrap_width = vw - 60  # rough label column margin
        if hasattr(self, "hm_backup"):
            self.hm_backup.set_wrap_width(wrap_width)
        if hasattr(self, "hm_mixed"):
            self.hm_mixed.set_wrap_width(wrap_width)
        if hasattr(self, "one_day_gap"):
            self.one_day_gap.set_wrap_width(wrap_width)

    def values(self) -> dict:
        return {
            "theme": self.theme_combo.currentText(),
            "font_size": self.font_spin.value(),
            "accent_color": self.accent_edit.text(),
            "show_gif": self.gif_chk.isChecked(),
            "calendar_grid": self.grid_chk.isChecked(),
            "weekend_gap_days": self.weekend_gap.value(),
            "min_days_between_assignments": self.min_between.value(),
            "main_score_factor": self.main_factor.value(),
            "backup_score_factor": self.backup_factor.value(),
            "availability_penalty": self.avail_penalty.value(),
            "history_window_days": self.hist_window.value(),
            "history_duration_months": self.hist_duration.value(),
            "measure_phase_times": self.measure_chk.isChecked(),
            "analyse_initial_weekday_gaps": self.analyse_chk.isChecked(),
            "gap_report_file": self.gap_file.text(),
            "assignment_debug_enabled": self.assignment_debug_chk.isChecked(),
            "debug_variant_logging": self.debug_mode_combo.currentData() or "off",
            "allow_post_weekend_wednesday_main": self.wed_main.isChecked(),
            "allow_post_weekend_wednesday_backup": self.wed_backup.isChecked(),
            "allow_post_weekend_thursday_main": self.thu_main.isChecked(),
            "allow_post_weekend_thursday_backup": self.thu_backup.isChecked(),
            "allow_midweek_pair_backup_only": self.hm_backup.isChecked(),
            "allow_midweek_pair_mixed": self.hm_mixed.isChecked(),
            # NEW master flag
            "allow_one_day_weekday_gap": self.one_day_gap.isChecked(),
        }
        
class SettingsDialog(ToolDialog):
    """
    Desktop Settings dialog (original layout) with a values() method and
    the two extra midweek one‑day gap toggles.
    """
    def __init__(self, settings, parent=None):
        super().__init__(parent, title="Settings")
        self.settings = settings
    
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        shell = QWidget()
        scroll.setWidget(shell)
        main_layout = QVBoxLayout(shell)
        main_layout.setContentsMargins(12, 12, 18, 12)
        main_layout.setSpacing(18)
    
        # UI group (original)
        ui_group = QGroupBox("UI")
        ui_layout = QGridLayout()
        self.theme_combo = QComboBox(); self.theme_combo.addItems(["dark","light","pink"]); self.theme_combo.setCurrentText(settings.get("theme"))
        self.font_spin = QSpinBox(); self.font_spin.setRange(8,32); self.font_spin.setValue(settings.get("font_size"))
        self.accent_edit = QLineEdit(settings.get("accent_color"))
        self.gif_chk = QCheckBox("Show animated GIF"); self.gif_chk.setChecked(settings.get("show_gif"))
        self.grid_chk = QCheckBox("Show calendar grid"); self.grid_chk.setChecked(settings.get("calendar_grid"))
        r=0
        for label, w in [
            ("Theme:", self.theme_combo),
            ("Font size:", self.font_spin),
            ("Accent color:", self.accent_edit),
        ]:
            ui_layout.addWidget(QLabel(label), r, 0); ui_layout.addWidget(w, r, 1); r+=1
        ui_layout.addWidget(self.gif_chk, r, 0, 1, 2); r+=1
        ui_layout.addWidget(self.grid_chk, r, 0, 1, 2)
        ui_group.setLayout(ui_layout)
        main_layout.addWidget(ui_group)
    
        # Scheduling group (original)
        sched_group = QGroupBox("Scheduling")
        sched_layout = QGridLayout()
        def sb(val, lo, hi):
            w = QSpinBox(); w.setRange(lo, hi); w.setValue(val); return w
        self.weekend_gap   = sb(settings.get("weekend_gap_days"), 7, 90)
        self.min_between   = sb(settings.get("min_days_between_assignments"), 0, 7)
        self.main_factor   = sb(settings.get("main_score_factor"), 1, 100)
        self.backup_factor = sb(settings.get("backup_score_factor"), 1, 100)
        self.avail_penalty = sb(settings.get("availability_penalty"), 0, 100)
        self.hist_window   = sb(settings.get("history_window_days"), 1, 365)
        self.hist_duration = sb(settings.get("history_duration_months"), 1, 60)
        r=0
        for label, w in [
            ("Weekend gap (days):", self.weekend_gap),
            ("Min days between:", self.min_between),
            ("Main score factor:", self.main_factor),
            ("Backup score factor:", self.backup_factor),
            ("Availability penalty:", self.avail_penalty),
            ("History window (days):", self.hist_window),
            ("History duration (months):", self.hist_duration),
        ]:
            sched_layout.addWidget(QLabel(label), r, 0); sched_layout.addWidget(w, r, 1); r+=1
        sched_group.setLayout(sched_layout)
        main_layout.addWidget(sched_group)

        # Debug / Diagnostics group
        debug_group = QGroupBox("Debug / Diagnostics")
        debug_layout = QGridLayout()
        self.measure_chk = QCheckBox("Measure phase times")
        self.measure_chk.setChecked(settings.get("measure_phase_times"))
        self.analyse_chk = QCheckBox("Analyse initial gaps")
        self.analyse_chk.setChecked(settings.get("analyse_initial_weekday_gaps"))
        self.assignment_debug_chk = QCheckBox("Enable structured assignment debug logging")
        self.assignment_debug_chk.setChecked(settings.get("assignment_debug_enabled"))
        self.debug_mode_combo = QComboBox()
        self.debug_mode_combo.addItem("Off", "off")
        self.debug_mode_combo.addItem("Pairs only", "pairs")
        self.debug_mode_combo.addItem("Variants only", "variants")
        self.debug_mode_combo.addItem("Pairs + variants", "all")
        dbg_idx = self.debug_mode_combo.findData(settings.get("debug_variant_logging"))
        if dbg_idx < 0:
            dbg_idx = 0
        self.debug_mode_combo.setCurrentIndex(dbg_idx)
        self.gap_file = QLineEdit(settings.get("gap_report_file"))
        rows = [
            (self.measure_chk, None),
            (self.analyse_chk, None),
            (self.assignment_debug_chk, None),
            (QLabel("Variant debug:"), self.debug_mode_combo),
            (QLabel("Gap report file:"), self.gap_file),
        ]
        r = 0
        for left, right in rows:
            if right is None:
                debug_layout.addWidget(left, r, 0, 1, 2)
            else:
                debug_layout.addWidget(left, r, 0)
                debug_layout.addWidget(right, r, 1)
            r += 1
        debug_group.setLayout(debug_layout)
        main_layout.addWidget(debug_group)

        # Relaxation group (original + new toggle)
        relax_group = QGroupBox("Post-Weekend Relaxations")
        relax_layout = QGridLayout()
        self.wed_main   = QCheckBox("Allow MAIN on Wednesday"); self.wed_main.setChecked(settings.get("allow_post_weekend_wednesday_main"))
        self.wed_backup = QCheckBox("Allow BACKUP on Wednesday"); self.wed_backup.setChecked(settings.get("allow_post_weekend_wednesday_backup"))
        self.thu_main   = QCheckBox("Allow MAIN on Thursday"); self.thu_main.setChecked(settings.get("allow_post_weekend_thursday_main"))
        self.thu_backup = QCheckBox("Allow BACKUP on Thursday"); self.thu_backup.setChecked(settings.get("allow_post_weekend_thursday_backup"))
    
        self.hm_backup = QCheckBox("Allow Mon–Wed / Tue–Thu one-day gap (both BACKUP)")
        self.hm_backup.setChecked(settings.get("allow_midweek_pair_backup_only"))
        self.hm_mixed  = QCheckBox("Allow Mon–Wed / Tue–Thu one-day gap (MAIN + BACKUP)")
        self.hm_mixed.setChecked(settings.get("allow_midweek_pair_mixed"))
    
        # NEW: master toggle
        self.one_day_gap = QCheckBox("Enable one-day weekday gap fallback (Mon–Wed / Tue–Thu)")
        self.one_day_gap.setChecked(settings.get("allow_one_day_weekday_gap"))
    
        for i,w in enumerate([self.wed_main, self.wed_backup, self.thu_main, self.thu_backup,
                              self.hm_backup, self.hm_mixed, self.one_day_gap]):
            relax_layout.addWidget(w, i, 0, 1, 2)
        relax_group.setLayout(relax_layout)
        main_layout.addWidget(relax_group)
    
        # Scoring Weights group (original)
        score_group = QGroupBox("Scoring Weights (normalized internally)")
        score_layout = QGridLayout()
        wdict = settings.get("scoring_weights")
        def wspin(val):
            w = QDoubleSpinBox(); w.setDecimals(3); w.setRange(0.0, 1.0); w.setSingleStep(0.05); w.setValue(float(val)); return w
        self.w_rotrep   = wspin(wdict.get("rotation_rep", 0.30))
        self.w_gaps     = wspin(wdict.get("gaps",         0.20))
        self.w_rotviol  = wspin(wdict.get("rot_viol",     0.15))
        self.w_wgap     = wspin(wdict.get("weekend_gap",  0.15))
        self.w_balance  = wspin(wdict.get("balance",      0.10))
        self.w_longterm = wspin(wdict.get("long_term",    0.10))
        labels = ["Rotation repeats:", "Weekday gaps:", "Rotation violations:", "Weekend gap:", "Balance:", "Long-term:"]
        spins  = [self.w_rotrep, self.w_gaps, self.w_rotviol, self.w_wgap, self.w_balance, self.w_longterm]
        for i,(lab,sp) in enumerate(zip(labels, spins)):
            score_layout.addWidget(QLabel(lab), i, 0); score_layout.addWidget(sp, i, 1)
        score_group.setLayout(score_layout)
        main_layout.addWidget(score_group)
    
        # Save / Cancel
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)
    
        outer = QVBoxLayout(); outer.addWidget(scroll)
        self.setLayout(outer)
        self.resize(600, 780)

    def values(self) -> dict:
        """Return all updated settings from this dialog."""
        return {
            "theme": self.theme_combo.currentText(),
            "font_size": self.font_spin.value(),
            "accent_color": self.accent_edit.text(),
            "show_gif": self.gif_chk.isChecked(),
            "calendar_grid": self.grid_chk.isChecked(),
            "weekend_gap_days": self.weekend_gap.value(),
            "min_days_between_assignments": self.min_between.value(),
            "main_score_factor": self.main_factor.value(),
            "backup_score_factor": self.backup_factor.value(),
            "availability_penalty": self.avail_penalty.value(),
            "history_window_days": self.hist_window.value(),
            "history_duration_months": self.hist_duration.value(),
            "measure_phase_times": self.measure_chk.isChecked(),
            "analyse_initial_weekday_gaps": self.analyse_chk.isChecked(),
            "gap_report_file": self.gap_file.text(),
            "assignment_debug_enabled": self.assignment_debug_chk.isChecked(),
            "debug_variant_logging": self.debug_mode_combo.currentData() or "off",
            "allow_post_weekend_wednesday_main": self.wed_main.isChecked(),
            "allow_post_weekend_wednesday_backup": self.wed_backup.isChecked(),
            "allow_post_weekend_thursday_main": self.thu_main.isChecked(),
            "allow_post_weekend_thursday_backup": self.thu_backup.isChecked(),
            # Legacy toggles (kept)
            "allow_midweek_pair_backup_only": self.hm_backup.isChecked(),
            "allow_midweek_pair_mixed": self.hm_mixed.isChecked(),
            # NEW master toggle
            "allow_one_day_weekday_gap": self.one_day_gap.isChecked(),
            "scoring_weights": {
                "rotation_rep": float(self.w_rotrep.value()),
                "gaps":         float(self.w_gaps.value()),
                "rot_viol":     float(self.w_rotviol.value()),
                "weekend_gap":  float(self.w_wgap.value()),
                "balance":      float(self.w_balance.value()),
                "long_term":    float(self.w_longterm.value()),
            },
        }
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


class PinkHeaderView(QHeaderView):
    """
    Legacy class kept only so runtime imports don’t fail.
    It now just calls the base implementation – all header styling is
    done by apply_theme_* helpers.
    """
    def __init__(self, orientation, parent=None, *_, **__):
        super().__init__(orientation, parent)

    # Qt will still call this, but we delegate straight to the original.
    def paintSection(self, painter, rect, logicalIndex):
        super().paintSection(painter, rect, logicalIndex)
        
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


class MultiDatePickerGrid(QCalendarWidget):
    """
    Internal grid used by MultiDatePicker.

    • Accent-colored blocks mark every ISO date in self.selected
    • No visible "today"/keyboard-focus/Qt-selection rectangle
    • Tapping still toggles membership in self.selected
    """
    ACCENT = "#5C8DBC"          # parent may override with setAccent()

    # ---------------------------------------------------------------------
    def __init__(self, selected=None, *, accent=None, theme="pink", parent=None):
        super().__init__(parent)
        if accent:
            self.ACCENT = accent
        self._theme = theme

        # ── basic appearance ───────────────────────────────────────────
        self.setGridVisible(True)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.NoHorizontalHeader)
        self.setNavigationBarVisible(False)            # wrapper has its own arrows
        self.setSelectionMode(QCalendarWidget.SingleSelection)

        # Apply theme-aware styling
        self._apply_theme()

        # remove Qt's default "today" outline
        self.setDateTextFormat(QDate.currentDate(), QTextCharFormat())

        # ── pre-selected dates ─────────────────────────────────────────
        self.selected = set(selected or [])
        for iso in list(self.selected):
            qd = QDate.fromString(iso, "yyyy-MM-dd")
            if qd.isValid():
                self._highlight(qd)

        # tap → toggle
        self.clicked.connect(self._toggle)

    def _apply_theme(self):
        """Apply theme-specific styling to the calendar grid."""
        if self._theme == "dark":
            self.setStyleSheet("""
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #E8EAF0;
                    background: #252A32;
                    color: #E8EAF0;
                }
                QCalendarWidget QAbstractItemView {
                    background: #252A32;
                    color: #E8EAF0;
                    gridline-color: #3A404B;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #3A404B;
                }
            """)
        elif self._theme == "light":
            self.setStyleSheet("""
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #2C2A27;
                    background: #FFFFFF;
                    color: #2C2A27;
                }
                QCalendarWidget QAbstractItemView {
                    background: #FFFFFF;
                    color: #2C2A27;
                    gridline-color: #E1DDD6;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #E1DDD6;
                }
            """)
        else:  # pink theme
            self.setStyleSheet("""
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #4A4A4A;
                    background: #F7D7DF;
                    color: #4A4A4A;
                }
                QCalendarWidget QAbstractItemView {
                    background: #F7D7DF;
                    color: #4A4A4A;
                    gridline-color: #E9A9B8;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #E9A9B8;
                }
            """)

    # ------------------------------------------------------------------
    #  external helpers
    # ------------------------------------------------------------------
    def setAccent(self, col: str):
        """Allow live-re-styling from MainWindow settings."""
        if col and col != self.ACCENT:
            self.ACCENT = col
            for iso in list(self.selected):
                qd = QDate.fromString(iso, "yyyy-MM-dd")
                self._highlight(qd)

    def set_theme(self, theme: str, accent: str):
        """Update theme for this grid."""
        self._theme = theme
        self.ACCENT = accent
        self._apply_theme()
        # Re-highlight all selected dates with new accent
        for iso in list(self.selected):
            qd = QDate.fromString(iso, "yyyy-MM-dd")
            if qd.isValid():
                self._highlight(qd)

    # ------------------------------------------------------------------
    #  toggling logic
    # ------------------------------------------------------------------
    def _toggle(self, qd: QDate):
        iso = qd.toString("yyyy-MM-dd")
        if iso in self.selected:
            self.selected.remove(iso)
            self.setDateTextFormat(qd, QTextCharFormat())   # clear
        else:
            self.selected.add(iso)
            self._highlight(qd)

    def _highlight(self, qd: QDate):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self.ACCENT))
        fmt.setForeground(Qt.white)
        fmt.setFontWeight(QFont.Bold)
        self.setDateTextFormat(qd, fmt)

    # ------------------------------------------------------------------
    #  paint-override → hide selection, draw our own accent blocks
    # ------------------------------------------------------------------
    def paintCell(self, painter, rect, qdate):
        # Draw default cell first (day number, grid, etc.)
        super().paintCell(painter, rect, qdate)

        iso = qdate.toString("yyyy-MM-dd")

        if iso in self.selected:                     # ACCENT BLOCK
            painter.save()
            painter.fillRect(rect, QColor(self.ACCENT))
            painter.setPen(Qt.white)
            f = painter.font(); f.setBold(True); painter.setFont(f)
            painter.drawText(rect, Qt.AlignCenter, str(qdate.day()))
            painter.restore()

        elif qdate == self.selectedDate():           # erase Qt selection
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.palette().base())
            painter.drawRect(rect)                   # cover any default selection
            painter.setPen(self.palette().text().color())
            painter.drawText(rect, Qt.AlignCenter, str(qdate.day()))
            painter.restore()

    # widen a little so grid looks nicer on phones
    def sizeHint(self):
        s = super().sizeHint()
        return QSize(max(440, s.width()), s.height())
        
        
class MultiDatePicker(QWidget):
    """
    Month-header + day-of-week bar + MultiDatePickerGrid.
    Navigation arrows pick a themed PNG automatically.
    """
    _HEADER_FONT = QFont("Roboto", 20, QFont.Bold)
    _DOW_FONT    = QFont("Roboto", 15, QFont.Bold)

    def __init__(self, selected=None, *, accent="#5C8DBC",
                 theme="pink", parent=None):
        super().__init__(parent)
        self._ACCENT = accent
        self._THEME  = theme

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # 1) calendar grid -------------------------------------------------
        self.cal = MultiDatePickerGrid(selected, accent=accent, theme=theme)
        root.addWidget(self.cal, 1)

        # 2) month navigation bar ------------------------------------------
        nav = QHBoxLayout(); nav.setContentsMargins(0, 0, 0, 0); nav.setSpacing(0)
        # keep references to nav buttons so theme updates can refresh icons
        self._btn_prev = self._arrow(nav, "arrowL.png", prev=True)
        self._lbl_month = QLabel(alignment=Qt.AlignCenter, font=self._HEADER_FONT)
        nav.addWidget(self._lbl_month, 1)
        self._btn_next = self._arrow(nav, "arrowR.png", prev=False)
        root.insertLayout(0, nav)

        # 3) day-of-week strip ---------------------------------------------
        dow = QHBoxLayout(); dow.setContentsMargins(0, 0, 0, 0); dow.setSpacing(0)
        self._dow_labels = []
        for i, txt in enumerate(("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")):
            lbl = QLabel(txt, alignment=Qt.AlignCenter, font=self._DOW_FONT)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            dow.addWidget(lbl); self._dow_labels.append(lbl)
        root.insertLayout(1, dow)

        self._apply_theme()
        self._refresh_month()
        self.cal.currentPageChanged.connect(self._refresh_month)

    # ------------------------- theming helpers --------------------------
    def _apply_theme(self):
        month_clr = {"dark": "#E8EAF0",
                     "light": "#2C2A27"}.get(self._THEME, self._ACCENT)
        self._lbl_month.setStyleSheet(f"color:{month_clr};")
        for i, lbl in enumerate(self._dow_labels):
            fg = "#E53935" if i in (0, 6) else "#FFFFFF"
            lbl.setStyleSheet(f"background:{self._ACCENT};color:{fg};")

    # ------------------------- public API --------------------------------
    @property
    def selected(self) -> set[str]:
        return self.cal.selected

    def setAccent(self, color: str):
        self._ACCENT = color
        self.cal.setAccent(color)
        self._apply_theme()

    def set_theme(self, theme: str, accent: str):
        self._THEME, self._ACCENT = theme, accent
        self.cal.set_theme(theme, accent)
        self._apply_theme()
        if hasattr(self, "_btn_prev") and hasattr(self, "_btn_next"):
            self._btn_prev.setIcon(themed_icon("arrowL.png", self._THEME))
            self._btn_next.setIcon(themed_icon("arrowR.png", self._THEME))

    # ------------------------- internals ---------------------------------
    def _arrow(self, layout: QHBoxLayout, png: str, *, prev: bool):
        btn = QPushButton(flat=True, cursor=Qt.PointingHandCursor)
        btn.setFixedSize(56, 56)
        btn.setIcon(themed_icon(png, self._THEME))
        btn.setIconSize(QSize(44, 44))
        btn.setStyleSheet("border:none;background:transparent;")
        btn.clicked.connect(self.cal.showPreviousMonth if prev
                            else self.cal.showNextMonth)
        btn.clicked.connect(self._refresh_month)
        layout.addWidget(btn)
        return btn

    def _refresh_month(self, *_):
        y, m = self.cal.yearShown(), self.cal.monthShown()
        self._lbl_month.setText(QDate(y, m, 1).toString("MMMM  yyyy"))

    def sizeHint(self):
        return self.cal.sizeHint()
                 
class SingleDatePicker(QWidget):
    """
    One-month calendar with external navigation bar.  Uses themed arrows.
    """
    FONT_HDR = QFont("Roboto", 20, QFont.Bold)
    FONT_DOW = QFont("Roboto", 15, QFont.Bold)
    FONT_GRID = QFont("Roboto", 15)

    def __init__(self, *, accent="#5C8DBC", parent=None,
                 initial=None, theme="pink"):
        super().__init__(parent)
        self.ACCENT = accent
        self._theme = theme
        self._current = initial or QDate.currentDate()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # calendar widget --------------------------------------------------
        self.cal = QCalendarWidget()
        self.cal.setGridVisible(True)
        self.cal.setSelectionMode(QCalendarWidget.SingleSelection)
        self.cal.setNavigationBarVisible(False)
        self.cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.cal.setHorizontalHeaderFormat(QCalendarWidget.NoHorizontalHeader)
        self.cal.setFont(self.FONT_GRID)
        self.cal.setSelectedDate(self._current)

        # navigation bar ---------------------------------------------------
        nav = QHBoxLayout(); nav.setSpacing(0)
        # store references so theme changes can refresh icons
        self.prev_btn = self._nav_btn(nav, "arrowL.png", prev=True)
        self.lbl_month = QLabel(alignment=Qt.AlignCenter, font=self.FONT_HDR)
        nav.addWidget(self.lbl_month, 1)
        self.next_btn = self._nav_btn(nav, "arrowR.png", prev=False)

        # day-of-week strip ------------------------------------------------
        dow = QHBoxLayout(); dow.setSpacing(0)
        self.dow_labels = []
        for i, d in enumerate(("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")):
            lbl = QLabel(d, alignment=Qt.AlignCenter, font=self.FONT_DOW)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            dow.addWidget(lbl); self.dow_labels.append(lbl)

        root.addLayout(nav); root.addLayout(dow); root.addWidget(self.cal, 1)

        self._apply_theme()
        self._highlight(self._current)
        self._update_month()
        self.cal.clicked.connect(self._on_click)
        self.cal.currentPageChanged.connect(self._update_month)

    # ------------------------- theming -----------------------------------
    def _apply_theme(self):
        apply_theme_to_calendar(self.cal, self._theme, self.ACCENT)
        month_clr = {"dark": "#E8EAF0", "light": "#2C2A27"}.get(self._theme,
                                                                self.ACCENT)
        self.lbl_month.setStyleSheet(f"color:{month_clr};")
        for i, lbl in enumerate(self.dow_labels):
            fg = "#E53935" if i in (0, 6) else "#FFFFFF"
            lbl.setStyleSheet(f"background:{self.ACCENT};color:{fg};")

    def set_theme(self, theme: str, accent: str):
        self._theme, self.ACCENT = theme, accent
        self._apply_theme()
        self._highlight(self._current)
        if hasattr(self, "prev_btn") and hasattr(self, "next_btn"):
            self.prev_btn.setIcon(themed_icon("arrowL.png", self._theme))
            self.next_btn.setIcon(themed_icon("arrowR.png", self._theme))

    # ------------------------- nav buttons -------------------------------
    def _nav_btn(self, layout: QHBoxLayout, png: str, *, prev: bool):
        b = QPushButton(flat=True, cursor=Qt.PointingHandCursor)
        b.setFixedSize(56, 56)
        b.setIcon(themed_icon(png, self._theme))
        b.setIconSize(QSize(44, 44))
        b.setStyleSheet("border:none;background:transparent;")
        b.clicked.connect(self.cal.showPreviousMonth if prev else self.cal.showNextMonth)
        b.clicked.connect(self._update_month)
        layout.addWidget(b)
        return b

    # ------------------------- helpers -----------------------------------
    def _update_month(self, *_):
        y, m = self.cal.yearShown(), self.cal.monthShown()
        self.lbl_month.setText(QDate(y, m, 1).toString("MMMM  yyyy"))

    def _highlight(self, qd: QDate):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self.ACCENT))
        fmt.setForeground(Qt.white)
        fmt.setFontWeight(QFont.Bold)
        self.cal.setDateTextFormat(qd, fmt)

    def _clear(self, qd): self.cal.setDateTextFormat(qd, QTextCharFormat())

    def _on_click(self, qd):
        if qd != self._current:
            self._clear(self._current)
            self._current = qd
            self._highlight(qd)

    # public helpers ------------------------------------------------------
    def iso(self)  -> str   : return self._current.toString("yyyy-MM-dd")
    def qdate(self) -> QDate: return QDate(self._current)

    def sizeHint(self):
        s = self.cal.sizeHint()
        return QSize(max(440, s.width()), s.height() + 70)
        
        
from PySide6.QtWidgets import (
    QSpacerItem, QStyleOptionHeader, QStyle
)
import pandas as pd
import time

class MultiLineHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None, height=56):
        super().__init__(orientation, parent)
        self._custom_height = height

    def paintSection(self, painter, rect, logicalIndex):
        if not rect.isValid():
            return
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = rect
        opt.section = logicalIndex
        self.style().drawControl(QStyle.CE_Header, opt, painter, self)
        painter.save()
        painter.setFont(self.font())
        painter.setPen(Qt.black)  # or your theme color
        txt = self.model().headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        if txt is not None:
            painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, str(txt))
        painter.restore()
        
    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(self._custom_height)
        return sh
        
from PySide6.QtWidgets import (
    QDialog, QDoubleSpinBox,
)
from PySide6.QtCore import Qt        

class RotationViolationDialog(ToolDialog):
    """
    Dialog for enabling/disabling rotation-rule violations, selecting which nurses may violate,
    and featuring large, clearly-outlined checkboxes that toggle on row click.
    """

    def __init__(self, parent, backend, accent=None, theme=None):
        super().__init__(parent, title="Rotation Violation Settings")

        # Get sorted summary from backend
        summary = backend.weekend_history.get_violation_summary()
        summary = summary.sort_values(
            by=["total_viol", "consec_viol", "clean_run_weeks", "days_since_last"],
            ascending=[True, True, False, False]
        ).reset_index(drop=True)
        self.nurses = summary["nurse"].tolist()
        self.stats = summary

        self._accent = (
            accent or getattr(parent, "settings", {}).get("accent_color", "#5C8DBC")
        )
        self._row_chk = []

        # ────────────── Layout ──────────────
        root = QVBoxLayout(self._body)
        root.setContentsMargins(18, 18, 18, 10)
        root.setSpacing(6)

        intro = QLabel(
            "Enable rotation violations, then tick the nurses that are permitted "
            "to violate.  Leave all boxes unchecked if NO nurse may violate.",
            wordWrap=True, font=QFont("Roboto", 14)
        )
        root.addWidget(intro)

        self.allow_chk = QCheckBox(
            "Allow rotation violations for this run?",
            font=QFont("Roboto", 15, QFont.Bold)
        )
        root.addWidget(self.allow_chk)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setFixedWidth(110)
        self.select_all_btn.setVisible(len(self.nurses) > 8)
        root.addWidget(self.select_all_btn, alignment=Qt.AlignLeft)

        # ────────────── Nurse list ──────────────
        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 14))
        self.list.setAlternatingRowColors(False)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setSpacing(0)
        self.list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.list.setStyleSheet("QListWidget::item:selected { background: transparent; }")

        # Touch/kinetic scrolling if available
        try:
            from PySide6.QtWidgets import QScroller
            QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)
        except Exception:
            pass

        # Checkbox style
        chk_style = f"""
        QCheckBox::indicator         {{ width:28px; height:28px; }}
        QCheckBox::indicator:unchecked {{
            border:2px solid #000;
            background:#fdfdfd;
            border-radius:4px;
        }}
        QCheckBox::indicator:checked  {{
            border:2px solid #000;
            background:{self._accent};
            border-radius:4px;
        }}
        """

        # Show all stats in label
        for i, row in summary.iterrows():
            n = row["nurse"]
            label = (
                f"{n}  (Viol: {row['total_viol']}, Streak: {row['consec_viol']}, "
                f"Clean: {row['clean_run_weeks']}, Days: {row['days_since_last']})"
            )
            roww = QWidget()
            hl = QHBoxLayout(roww)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)
            cb = QCheckBox();  cb.setFixedSize(28, 28);  cb.setStyleSheet(chk_style)
            lbl = QLabel(label)
            lbl.setFont(QFont("Roboto", 14))
            hl.addWidget(cb);  hl.addWidget(lbl);  hl.addStretch()
            itm = QListWidgetItem();  itm.setSizeHint(roww.sizeHint())
            self.list.addItem(itm);    self.list.setItemWidget(itm, roww)
            self._row_chk.append(cb)

        # List height logic
        hard_cap = 700
        row_h = self.list.sizeHintForRow(0) or 36
        if row_h * len(self.nurses) > hard_cap:
            self.list.setMaximumHeight(hard_cap)
        root.addWidget(self.list, stretch=1)

        # Clicking any row toggles its checkbox
        self.list.itemClicked.connect(self._toggle_row)

        # ────────────── OK / Cancel ──────────────
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        h = QHBoxLayout(); h.addStretch(); h.addWidget(btn_box); h.addStretch()
        root.addLayout(h)

        # Dialog window sizing
        self.setMinimumWidth(300);  self.setMaximumWidth(420)
        self.setMinimumHeight(500); self.setMaximumHeight(1200)

        self._wire_signals()
        self._apply_accent()

    def _wire_signals(self):
        self.allow_chk.toggled.connect(self.list.setEnabled)
        self.list.setEnabled(False)
        self.select_all_btn.clicked.connect(
            lambda: [cb.setChecked(True) for cb in self._row_chk]
        )

    def _toggle_row(self, item):
        idx = self.list.row(item)
        if 0 <= idx < len(self._row_chk):
            cb = self._row_chk[idx]
            cb.setChecked(not cb.isChecked())
        self.list.clearSelection()

    def _apply_accent(self):
        self.select_all_btn.setStyleSheet(
            (
                "QPushButton {"
                f"background:{self._accent};"
                "color:#fff;"
                "border-radius:8px;"
                "padding:6px 12px;"
                "}"
                "QPushButton:pressed {"
                "opacity:0.8;"
                "}"
            )
        )

    def refresh_accent(self, accent=None):
        if accent:
            self._accent = accent
        self._apply_accent()

    def set_theme(self, theme, accent):
        self._accent = accent
        self._apply_accent()

    def get_values(self):
        if not self.allow_chk.isChecked():
            return (False, [])
        allowed = [
            self.nurses[i]
            for i, cb in enumerate(self._row_chk)
            if cb.isChecked()
        ]
        return (True, allowed)
        
class VariantReviewDialog(ToolDialog):
    """
    Touch-friendly review of schedule variants. Prev/Next arrows obey theme.
    """
    _ROW_H = 18
    _HEAD_FONT = QFont("Roboto", 14, QFont.Bold)
    _CELL_FONT = QFont("Roboto", 12)

    def __init__(self, parent, variants, weekend_history, assignment_history, backup):
        super().__init__(parent, title="Review Schedules")
        self.variants = variants
        self.wh = weekend_history
        self.ah = assignment_history
        self._backup = backup
        self._cur = 0
        self._build_ui()
        # Defer initial paint to avoid blank first render on Android/Qt
        QTimer.singleShot(0, self._update_page)
        self.showMaximized()

    # ----------------------- UI build -----------------------------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # header ----------------------------------------------------------
        self.header = QLabel(alignment=Qt.AlignCenter, font=self._HEAD_FONT)
        outer.addWidget(self.header)

        # helper to make flick-scrollable tables -------------------------
        def _make_tbl(cols: int, headers: list[str]) -> QTableWidget:
            tbl = QTableWidget(0, cols, self)
            tbl.setHorizontalHeaderLabels(headers)
            tbl.setFont(self._CELL_FONT)
            tbl.verticalHeader().setDefaultSectionSize(self._ROW_H)
            tbl.horizontalHeader().setFixedHeight(self._ROW_H + 2)
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tbl.setSelectionMode(QAbstractItemView.NoSelection)
            tbl.setAlternatingRowColors(True)
            tbl.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            vp = tbl.viewport()
            vp.setAttribute(Qt.WA_AcceptTouchEvents, True)
            for g in (QScroller.TouchGesture, QScroller.LeftMouseButtonGesture):
                QScroller.grabGesture(vp, g)
            return tbl

        # schedule table --------------------------------------------------
        self.schedule_tbl = _make_tbl(3, ["Date", "Main", "Backup"])
        hh = self.schedule_tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.schedule_tbl.setColumnWidth(0, 100)
        outer.addWidget(self.schedule_tbl, 3)

        # counts table ----------------------------------------------------
        self.counts_tbl = _make_tbl(4, ["Nurse", "Main", "Backup", "Total"])
        for c in range(4):
            self.counts_tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch)
        outer.addWidget(self.counts_tbl, 2)

        # navigation buttons ---------------------------------------------
        nav = QHBoxLayout(); nav.setSpacing(16); nav.addStretch()
        theme = (self.parent().settings.get("theme")
                 if hasattr(self.parent(), "settings") else "pink")

        self.prev_btn = QPushButton("Previous")
        self.prev_btn.setMinimumWidth(110)
        self.prev_btn.setIcon(themed_icon("arrowL.png", theme))
        self.prev_btn.setIconSize(QSize(24, 24))
        self.prev_btn.setLayoutDirection(Qt.LeftToRight)

        self.next_btn = QPushButton("Next")
        self.next_btn.setMinimumWidth(110)
        self.next_btn.setIcon(themed_icon("arrowR.png", theme))
        self.next_btn.setIconSize(QSize(24, 24))
        self.next_btn.setLayoutDirection(Qt.RightToLeft)

        nav.addWidget(self.prev_btn); nav.addSpacing(20); nav.addWidget(self.next_btn)
        nav.addStretch(); outer.addLayout(nav)

        # action buttons --------------------------------------------------
        act = QHBoxLayout(); act.setSpacing(16); act.addStretch()
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")

        # Optional: Calendar View button (exports HTML; safe to leave)
        self.calendar_btn = QPushButton("Calendar View")
        self.calendar_btn.clicked.connect(lambda: export_variants_calendar_html(self.variants, max_variants=5))

        # Order: Calendar | Save | Cancel
        act.addWidget(self.calendar_btn)
        act.addSpacing(12)
        act.addWidget(self.save_btn)
        act.addSpacing(20)
        act.addWidget(self.cancel_btn)
        act.addStretch()
        outer.addLayout(act)

        # signals ---------------------------------------------------------
        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn.clicked.connect(self.reject)

    # ───────────────────── page refresh helpers ──────────────────────
    def _parse_variant(self, var):
        """Return idx, metrics dict, counts dict, df (counts built if missing)."""
        idx, metrics = var[0], var[1]
        if len(var) >= 4:                    # counts explicitly provided
            counts, df = var[2], var[3]
        else:                                # compute counts from dataframe
            df = var[2]
            counts = {}
            for _, row in df.iterrows():
                for role in ("main", "backup"):
                    nurse = row.get(role)
                    if nurse:
                        counts.setdefault(nurse, {"main": 0, "backup": 0, "total": 0})
                        counts[nurse][role] += 1
                        counts[nurse]["total"] += 1
        return idx, metrics, counts, df

    def _update_page(self):
        total = len(self.variants)
        print(f"[review] cur={self._cur} total={total} rows={len(self._parse_variant(self.variants[self._cur])[3]) if total else 0}")

        # Guard for empty list
        if total == 0:
            self.header.setText("No schedules to display.")
            self.schedule_tbl.setRowCount(0)
            self.counts_tbl.setRowCount(0)
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.save_btn.setEnabled(False)
            return  # <-- return only when truly empty

        # Theme-safe text color so cells never appear "blank"
        theme = (self.parent().settings.get("theme")
                 if hasattr(self.parent(), "settings") else "pink")
        fg = QColor("#E8EAF0") if theme == "dark" else QColor("#2C2A27")

        idx, st, counts, df = self._parse_variant(self.variants[self._cur])

        self.header.setText(
            f"Variant {self._cur + 1}/{total} • gaps {st.get('gaps', '—')}"
            f" • Δmain {st.get('balance_main', '—')}"
            f" • Δbackup {st.get('balance_backup', '—')}"
        )

        # schedule table
        self.schedule_tbl.setRowCount(len(df))
        for r, (dt, row) in enumerate(df.iterrows()):
            values = (dt.date().isoformat(), row.get("main", "-"), row.get("backup", "-"))
            for c, val in enumerate(values):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)  # read-only
                itm.setTextAlignment(Qt.AlignCenter if c == 0 else Qt.AlignVCenter | Qt.AlignLeft)
                itm.setForeground(fg)           # ensure visible text
                self.schedule_tbl.setItem(r, c, itm)

        # counts table (max 9 visible rows)
        nurses = sorted(counts)
        self.counts_tbl.setRowCount(len(nurses))
        for r, n in enumerate(nurses):
            m = counts[n]["main"]; b = counts[n]["backup"]; t = counts[n]["total"]
            for c, val in enumerate((n, m, b, t)):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)
                itm.setTextAlignment(Qt.AlignCenter)
                itm.setForeground(fg)           # ensure visible text
                self.counts_tbl.setItem(r, c, itm)

        head_h = self.counts_tbl.horizontalHeader().height()
        visible = min(len(nurses), 9)
        self.counts_tbl.setFixedHeight(head_h + visible * self._ROW_H)

        # nav buttons
        self.prev_btn.setEnabled(self._cur > 0)
        self.next_btn.setEnabled(self._cur < total - 1)

    # ───────────────────────── navigation slots ───────────────────────
    def _prev(self):
        if self._cur:
            self._cur -= 1
            self._update_page()

    def _next(self):
        if self._cur < len(self.variants) - 1:
            self._cur += 1
            self._update_page()

    # ───────────────────────── save slot ──────────────────────────────
    def _save(self):
        _, _, _, df = self._parse_variant(self.variants[self._cur])
        # commit weekend + history (keeps your original logic)
        for dt, row in df.iterrows():
            main, backup = row.get("main"), row.get("backup")
            if dt.weekday() == 4 and main and backup and main != backup:
                self.wh.modify_assignment(dt.isoformat(), main, backup)
            self.ah.update_history(dt.isoformat(), main, backup)

        show_info(self, "Saved",
                  "Schedule and history have been updated.")
        self.accept()

    def apply_theme_update(self):
        """Refresh navigation button icons when theme changes."""
        theme = (self.parent().settings.get("theme")
                 if hasattr(self.parent(), "settings") else "pink")
        self.prev_btn.setIcon(themed_icon("arrowL.png", theme))
        self.next_btn.setIcon(themed_icon("arrowR.png", theme))        
        
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

class TallHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None, height=64):
        super().__init__(orientation, parent)
        self._custom_height = height

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(self._custom_height)
        return sh


class AdvancedWeekendStatsScreen(QWidget):
    """Interactive, theme-aware statistics screen for weekend violations."""

    # ---- fonts & sizing ---------------------------------------------------
    BUTTON_HEIGHT     = 40
    BUTTON_MIN_WIDTH  = 150
    MIN_COL_WIDTH     = 80        # new: guarantees no header truncation

    BUTTON_FONT  = QFont("Roboto", 13, QFont.Bold)
    LABEL_FONT   = QFont("Roboto", 13)
    SECTION_FONT = QFont("Roboto", 16, QFont.Bold)
    TABLE_FONT   = QFont("Roboto", 12)
    HEADER_FONT  = QFont("Roboto", 13, QFont.Bold)
    TITLE_FONT   = QFont("Roboto", 20, QFont.Bold)

    # ---- table header text -----------------------------------------------
    HEADER_KEYS = [
        "Nurse",
        "V. Count",
        "Last\nViolation",
        "Viol.\nStreak",
        "Clean\nRuns",
        "Days\nSince",
    ]

    # ---- relative column weights -----------------------------------------
    COLUMN_WEIGHTS = [1, 1, 1, 1, 1, 1]   # first/last widened

    # ----------------------------------------------------------------------
    #                               life-cycle
    # ----------------------------------------------------------------------
    def __init__(self, parent):
        super().__init__(parent)
        self.parent         = parent
        self.backend        = parent.backend
        self._stats_date    = pd.Timestamp.today().strftime("%Y-%m-%d")
        self._current_nurse: Optional[str] = None
        self._press_time    = None
        self._press_row     = None

        self._build_ui()
        self.show_stats()               # initial render
        QTimer.singleShot(0, self._finalize_table_layout)  # first layout pass

    # ----------------------------------------------------------------------
    #                             UI construction
    # ----------------------------------------------------------------------
    def _build_ui(self) -> None:
        """Create every widget and wire up signals.  No illegal kwargs."""
        # ---------- root layout -------------------------------------------
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(18, 18, 18, 18)
    
        # ---------- title --------------------------------------------------
        title = QLabel("Rotation Violation Stats", alignment=Qt.AlignCenter)
        title.setFont(self.TITLE_FONT)
        root.addWidget(title)
    
        # ---------- date row ----------------------------------------------
        date_row = QHBoxLayout()
        self.btn_change_date = self._make_button("Change Date", fixed_w=130)
        self.btn_change_date.clicked.connect(self._open_date_picker)
        date_row.addWidget(self.btn_change_date, alignment=Qt.AlignLeft)
    
        date_row.addStretch()
    
        self.lbl_stats_date = QLabel("", alignment=Qt.AlignRight)
        self.lbl_stats_date.setFont(QFont("Roboto", 14, QFont.Bold))
        date_row.addWidget(self.lbl_stats_date)
        root.addLayout(date_row)
    
        # ---------- toolbar ------------------------------------------------
        bar = QHBoxLayout()
        bar.addStretch()
        self.btn_rebuild = self._make_button("Rebuild History")
        bar.addWidget(self.btn_rebuild)
        root.addLayout(bar)
    
    
        self.header_scroll = QScrollArea()
        self.header_scroll.setFixedHeight(48)
        self.header_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.header_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.header_scroll.setFrameShape(QFrame.NoFrame)
        self.header_scroll.setWidgetResizable(True)
        # FIXED: Ensure the scroll area can display full width content
        self.header_scroll.setMinimumWidth(0)  # Remove any minimum width constraints
    
        self.header_container = QWidget()
        self.header_row = QHBoxLayout(self.header_container)
        self.header_row.setSpacing(0)
        self.header_row.setContentsMargins(0, 0, 0, 0)
    
        self.header_labels = []
        for idx, txt in enumerate(self.HEADER_KEYS):
            lbl = QLabel(txt, wordWrap=True, alignment=Qt.AlignCenter)
            lbl.setFont(self.HEADER_FONT)
            lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            lbl.setFixedHeight(48)
            # FIXED: Ensure minimum width for each header
            lbl.setMinimumWidth(self.MIN_COL_WIDTH)
            # click-to-sort
            lbl.mousePressEvent = lambda _, col=idx: self._sort_by_column(col)
            self.header_row.addWidget(lbl)
            self.header_labels.append(lbl)
    
        self.header_scroll.setWidget(self.header_container)
        root.addWidget(self.header_scroll)
    
        # ---------- data table --------------------------------------------
        self.table = QTableWidget()
        self.table.setFont(self.TABLE_FONT)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setColumnCount(len(self.HEADER_KEYS))
        self.table.setSortingEnabled(True)
    
        # Use Fixed mode for manual width control
        hhdr = self.table.horizontalHeader()
        hhdr.setSectionResizeMode(QHeaderView.Fixed)
        
        # Allow horizontal scrolling if needed (will be managed by our layout)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    
        # keep header row in sync when the body scrolls horizontally
        self.table.horizontalScrollBar().valueChanged.connect(
            self._sync_header_scroll
        )
    
        root.addWidget(self.table, 1)
        root.setStretchFactor(self.table, 1)
    
        # ---------- selected nurse label ----------------------------------
        self.lbl_selected_nurse = QLabel("Selected Nurse: None", alignment=Qt.AlignLeft)
        self.lbl_selected_nurse.setFont(QFont("Roboto", 15, QFont.Bold))
        root.addWidget(self.lbl_selected_nurse)
    
        # ---------- edit area ---------------------------------------------
        root.addWidget(self._build_edit_group())
    
        # ---------- signals common to the screen --------------------------
        self.btn_rebuild.clicked.connect(self.rebuild_history)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.installEventFilter(self)
    
        # ---------- theme & first render ----------------------------------
        self.apply_theme_update()
        
    # ------------------------------------------------------------------ UI
    def _make_button(self, text: str, *, fixed_w: int | None = None) -> QPushButton:
        btn = QPushButton(text)
        btn.setFont(self.BUTTON_FONT)
        btn.setFixedHeight(self.BUTTON_HEIGHT)
        btn.setMinimumWidth(self.BUTTON_MIN_WIDTH)
        if fixed_w:
            btn.setFixedWidth(fixed_w)
        return btn

    def _build_edit_group(self) -> QGroupBox:
        """Return the populated QGroupBox used for editing a nurse."""
        edit_group = QGroupBox("Edit Selected Nurse")
        edit_group.setFont(self.SECTION_FONT)
    
        edit_grid = QGridLayout(edit_group)
        edit_grid.setHorizontalSpacing(10)
        edit_grid.setVerticalSpacing(6)
        edit_grid.setContentsMargins(14, 6, 14, 6)
    
        # --- violation count row ------------------------------------------
        lbl_vc = QLabel("Violation Count:")
        lbl_vc.setFont(self.LABEL_FONT)
    
        self.spn_violation = QSpinBox(edit_group)
        self.spn_violation.setRange(0, 99)
        self.spn_violation.setFont(self.LABEL_FONT)
        self.spn_violation.setFixedWidth(70)
    
        self.btn_save_cnt = self._make_button("Save Count")
        self.btn_save_cnt.clicked.connect(self.save_count)
    
        edit_grid.addWidget(lbl_vc,            0, 0, Qt.AlignRight)
        edit_grid.addWidget(self.spn_violation,0, 1)
        edit_grid.addWidget(self.btn_save_cnt, 0, 2)
    
        # --- last pattern row ---------------------------------------------
        lbl_lp = QLabel("Last Pattern:")
        lbl_lp.setFont(self.LABEL_FONT)
    
        self.cmb_pattern = QComboBox(font=self.LABEL_FONT)
        self.cmb_pattern.addItems(["FSF", "SFS"])
        self.cmb_pattern.setFixedWidth(90)
    
        self.btn_save_pat = self._make_button("Save Last Pattern")
        self.btn_save_pat.clicked.connect(self.save_pattern)
    
        edit_grid.addWidget(lbl_lp,            1, 0, Qt.AlignRight)
        edit_grid.addWidget(self.cmb_pattern,  1, 1)
        edit_grid.addWidget(self.btn_save_pat, 1, 2)
    
        # spacer
        edit_grid.addItem(
            QSpacerItem(1, 6, QSizePolicy.Minimum, QSizePolicy.Fixed), 2, 0
        )
    
        # back button
        self.btn_back = self._make_button("Back")
        self.btn_back.setFont(QFont("Roboto", 18, QFont.Bold))
        self.btn_back.clicked.connect(lambda: self.parent.switch_frame("main"))
        edit_grid.addWidget(self.btn_back, 3, 0, 1, 3)
    
        return edit_group

    # ----------------------------------------------------------------------
    #                         layout / resize helpers
    # ----------------------------------------------------------------------
    def _finalize_table_layout(self) -> None:
        """
        FIXED: Properly calculate and distribute column widths for perfect alignment.
        """
        # Ensure we have all expected headers
        if len(self.header_labels) != len(self.HEADER_KEYS):
            return
            
        # Get the widget's total available width 
        total_width = self.width() - 40  # Account for main layout margins (18*2 + buffer)
        
        # Calculate proportional widths
        total_weight = sum(self.COLUMN_WEIGHTS)
        
        # First pass: calculate basic proportional widths
        base_widths = []
        for weight in self.COLUMN_WEIGHTS:
            width = int(total_width * weight / total_weight)
            base_widths.append(width)
        
        # Second pass: apply minimum width constraints
        final_widths = []
        for i, base_width in enumerate(base_widths):
            # Get minimum width needed for this header's text
            if i < len(self.header_labels):
                text_metrics = self.header_labels[i].fontMetrics()
                text_width = text_metrics.boundingRect(self.HEADER_KEYS[i]).width() + 20
                min_needed = max(text_width, self.MIN_COL_WIDTH)
            else:
                min_needed = self.MIN_COL_WIDTH
                
            final_width = max(base_width, min_needed)
            final_widths.append(final_width)
        
        # Third pass: ensure total doesn't exceed available space
        current_total = sum(final_widths)
        if current_total > total_width:
            # Scale down proportionally if too wide
            scale = total_width / current_total
            final_widths = [int(w * scale) for w in final_widths]
            # Give any remainder to the last column
            remainder = total_width - sum(final_widths)
            final_widths[-1] += remainder
        
        # Apply the calculated widths
        header_total = 0
        for i, width in enumerate(final_widths):
            # Set table column width
            self.table.setColumnWidth(i, width)
            
            # Set matching header width
            if i < len(self.header_labels):
                self.header_labels[i].setFixedWidth(width)
                header_total += width
        
        # CRITICAL: Set the header container to exact calculated width
        self.header_container.setFixedWidth(header_total)
        self.header_container.setMinimumWidth(header_total)
        self.header_container.setMaximumWidth(header_total)
        
        # Force the scroll area to accommodate the full header width
        self.header_scroll.widget().adjustSize()
        
        # Set uniform row heights
        for r in range(self.table.rowCount()):
            self.table.setRowHeight(r, 22)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Use a longer delay to ensure layout is complete
        QTimer.singleShot(50, self._finalize_table_layout)

    def _sync_header_scroll(self, value: int) -> None:
        """
        Table's horizontal scroll-bar just moved; make the header scroll-area
        show the exact same offset so grid lines stay aligned.
        """
        self.header_scroll.horizontalScrollBar().setValue(value)
    
    def show_stats(self) -> None:
        """Populate the table for the currently selected stats date."""
        as_of = pd.to_datetime(self._stats_date)
        self.lbl_stats_date.setText(f"Stats as of: {as_of:%Y-%m-%d}")

        summary = self.backend.weekend_history.get_violation_summary(as_of=as_of)

        self.table.setRowCount(0)
        for r, row in enumerate(summary.itertuples(index=False)):
            last = (
                str(row.last_violation_date)[:10]
                if row.last_violation_date and str(row.last_violation_date) != "NaT"
                else ""
            )

            cells = [
                str(row.nurse),
                str(row.total_viol),
                last,
                str(row.consec_viol),
                str(row.clean_run_weeks),
                str(row.days_since_last),
            ]
            self.table.insertRow(r)
            for c, txt in enumerate(cells):
                itm = QTableWidgetItem(txt)
                itm.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if c > 0:
                    itm.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, itm)

        self._finalize_table_layout()
        self._restore_selection()
        self.refresh_edit_fields()
        self.apply_theme_update()

    def apply_theme_update(self) -> None:
        """Apply color palette based on the parent's theme settings."""
        theme  = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        if theme == "dark":
            table_bg, table_fg = "#252A32", "#E8EAF0"
            alt_bg,    grid    = "#2A3038", "#3A404B"
            sel_bg,    sel_fg  = accent,   "#FFFFFF"
            header_bg, header_fg = "#3A404B", "#E8EAF0"
            btn_base,  btn_press = "#3A404B", "#2D3238"
        elif theme == "light":
            table_bg, table_fg = "#FFFFFF", "#2C2A27"
            alt_bg,    grid    = "#FDFCFA", "#E1DDD6"
            sel_bg,    sel_fg  = accent,   "#FFFFFF"
            header_bg, header_fg = "#F5F3F0", "#2C2A27"
            btn_base,  btn_press = "#F5F3F0", "#E1DDD6"
        else:  # pink theme
            table_bg, table_fg = "#F7D7DF", "#4A4A4A"
            alt_bg,    grid    = "#FDEDEE", "#E9A9B8"
            sel_bg,    sel_fg  = "#FF85A1", "#FFFFFF"
            header_bg, header_fg = "#F9D1D9", "#4A4A4A"
            btn_base,  btn_press = "#F9D1D9", "#F6C3CE"

        # table style
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {table_bg};
                color: {table_fg};
                alternate-background-color: {alt_bg};
                gridline-color: {grid};
                selection-background-color: {sel_bg};
                selection-color: {sel_fg};
            }}
        """)

        # fake header labels
        header_css = (
            f"background:{header_bg};color:{header_fg};"
            f"font-weight:bold;font-size:13px;padding:4px;border:1px solid {grid};"
        )
        for lbl in self.header_labels:
            lbl.setStyleSheet(header_css)

        # button style
        btn_css = (
            "QPushButton {{ background:%s; color:%s; border-radius:8px; font-weight:bold; }}"
            "QPushButton:pressed {{ background:%s; }}"
        )
        btn_css = btn_css % (
            btn_base,
            header_fg if theme != "dark" else table_fg,
            btn_press,
        )
        for btn in [
            self.btn_change_date, self.btn_rebuild, self.btn_save_cnt,
            self.btn_save_pat,   self.btn_back
        ]:
            btn.setStyleSheet(btn_css)

    def _sort_by_column(self, col: int) -> None:
        """Toggle ascending/descending sort on the given column."""
        hdr = self.table.horizontalHeader()
        current = hdr.sortIndicatorSection()
        order   = hdr.sortIndicatorOrder()
        if current == col:  # toggle
            order = Qt.AscendingOrder if order == Qt.DescendingOrder else Qt.DescendingOrder
        else:
            order = Qt.AscendingOrder
        self.table.sortItems(col, order)
        self._restore_selection()

    def eventFilter(self, obj, event):
        if obj is self.table:
            if event.type() == QEvent.MouseButtonPress and event.buttons() & Qt.LeftButton:
                self._press_time = time.time()
                self._press_row  = self.table.indexAt(event.pos()).row()
            elif event.type() == QEvent.MouseButtonRelease and self._press_time is not None:
                if time.time() - self._press_time > 0.7 and self._press_row >= 0:
                    nurse = self.table.item(self._press_row, 0).text()
                    QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))
                self._press_time, self._press_row = None, None
        return super().eventFilter(obj, event)

    # ---------- context-menu & double-click --------------------------------
    def _on_table_cell_double_clicked(self, row, col):
        nurse = self.table.item(row, 0).text()
        QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

    def _on_table_context_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row >= 0:
            nurse = self.table.item(row, 0).text()
            QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

    # ---------- selection helpers -----------------------------------------
    def _on_table_selection(self):
        row = self.table.currentRow()
        self._current_nurse = self.table.item(row, 0).text() if row >= 0 else None
        self._update_selected_nurse_label()
        self.refresh_edit_fields()

    def _update_selected_nurse_label(self):
        txt = f"Selected Nurse: {self._current_nurse}" if self._current_nurse else "Selected Nurse: None"
        self.lbl_selected_nurse.setText(txt)

    def _restore_selection(self):
        if not self._current_nurse:
            self.table.clearSelection()
            return
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == self._current_nurse:
                self.table.selectRow(r)
                return
        self.table.clearSelection()
        self._current_nurse = None

    # ----------------------------------------------------------------------
    #                         nurse details popup
    # ----------------------------------------------------------------------
    def show_nurse_details(self, nurse: str):
        as_of = pd.to_datetime(self._stats_date)
        summary = self.backend.weekend_history.get_violation_summary(as_of=as_of)
        row = summary.loc[summary["nurse"] == nurse]
        if row.empty:
            return
        row = row.iloc[0]
        msg = (
            f"Nurse: {row.nurse}\n"
            f"Total Violations: {row.total_viol}\n"
            f"Last Violation: "
            f"{row.last_violation_date if str(row.last_violation_date) != 'NaT' else 'Never'}\n"
            f"Consecutive Violations: {row.consec_viol}\n"
            f"Clean Runs: {row.clean_run_weeks}\n"
            f"Days Since Last: {row.days_since_last}"
        )
        show_info(self, "Nurse Details", msg)

    def refresh_edit_fields(self):
        nurse = self._current_nurse
        enabled = nurse is not None
        self.spn_violation.setEnabled(enabled)
        self.cmb_pattern.setEnabled(enabled)
        self.btn_save_cnt.setEnabled(enabled)
        self.btn_save_pat.setEnabled(enabled)
        if not enabled:
            self.spn_violation.setValue(0)
            self.cmb_pattern.setCurrentIndex(0)
            return

        cnts = self.backend.weekend_history.get_violation_counts()
        self.spn_violation.setValue(cnts.get(nurse, 0))
        lp = self.backend.weekend_history.get_last_pattern(nurse)
        self.cmb_pattern.setCurrentText(lp.value if lp else "FSF")

    # ---------- actions ----------------------------------------------------
    def save_count(self):
        nurse = self._current_nurse
        if not nurse:
            show_info(self, "Select Nurse", "Choose a nurse first.")
            return
        try:
            self.backend.weekend_history.set_violation_count(nurse, self.spn_violation.value())
            show_info(self, "Saved", f"Violation count updated for {nurse}.")
            self.show_stats()
        except Exception as exc:  # pragma: no cover
            show_info(self, "Error", str(exc))

    def save_pattern(self):
        nurse = self._current_nurse
        if not nurse:
            show_info(self, "Select Nurse", "Choose a nurse first.")
            return
        try:
            pat = WeekendPattern(self.cmb_pattern.currentText())
            self.backend.weekend_history.set_last_pattern(nurse, pat)
            show_info(self, "Saved", f"Last pattern updated for {nurse}.")
            self.show_stats()
        except Exception as exc:  # pragma: no cover
            show_info(self, "Error", str(exc))

    # ---------- rebuild history -------------------------------------------
    def rebuild_history(self):
        def _start_rebuild():
            self.progress = QProgressDialog(
                "Rebuilding violation history…", None, 0, 0, self,
                windowTitle="Please Wait",
                windowModality=Qt.WindowModal,
            )
            self.progress.setCancelButton(None)
            self.progress.setMinimumDuration(0)
            self.progress.show()

            self.worker = RebuildViolationWorker(self.backend.weekend_history)
            self.worker.finished.connect(self._on_rebuild_done)
            self.worker.start()

        confirm(
            self,
            "Confirm",
            "Rebuild violation history from weekend assignments?",
            yes_cb=_start_rebuild,
            cancel_text="No",
        )

    def _on_rebuild_done(self, success: bool, msg: str):
        self.progress.cancel()
        self.worker = None
        show_info(self, "Done" if success else "Error", msg)
        if success:
            self.show_stats()

    def _open_date_picker(self):
        """Modal picker that lets the user change the stats date."""
        dlg = ToolDialog(self, "Select Stats Date")
    
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
    
        picker = SingleDatePicker(
            accent=self.parent.settings.get("accent_color"),
            initial=QDate.fromString(self._stats_date, "yyyy-MM-dd"),
            theme=self.parent.settings.get("theme"),
        )
        layout.addWidget(picker)
    
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(lambda: self._on_date_selected(picker, dlg))
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
    
        dlg.setLayout(layout)
        dlg.open()

    @Slot()
    def _on_date_selected(self, picker, dlg):
        self._stats_date = picker.iso()   # SingleDatePicker supplies ISO-YYYY-MM-DD
        dlg.accept()
        self.show_stats()
        
class MainMenu(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent   = parent
        self.settings = parent.settings

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        title = QLabel("Nurse Scheduler")
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        # animated GIF
        self.gif_lbl = QLabel(alignment=Qt.AlignCenter)
        self.gif_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.gif_lbl)
        self.movie = QMovie("GIF.gif")
        self.movie.setCacheMode(QMovie.CacheAll)
        self.movie.setSpeed(100)
        self.gif_lbl.setMovie(self.movie)
        self._aspect = 16 / 9
        self.movie.frameChanged.connect(self._capture_aspect_once)
        self.update_gif(self.settings.get("show_gif"))
        lay.addSpacing(8)

        # buttons
        for text, name in [
            ("Manage Nurses",            "manage"),
            ("Pre-scheduled Assignments","prescheduled"),
            ("Assignment History",       "assignment_hist"),
            ("Weekend History",          "weekend_history"),
            ("View Unavailable Dates",   "view_unavail"),
            ("Generate Schedule",        "generate"),
            ("Advanced Weekend Stats",   "advanced_stats"),  # <-- NEW BUTTON
            ("Settings",                 "settings"),
            ("Quit",                     None)
        ]:
            btn = QPushButton(text)
            btn.setMinimumHeight(40)
            if name == "settings":
                btn.setProperty("role", "special")
                btn.clicked.connect(parent.open_settings_dialog)
            elif name:
                btn.clicked.connect(lambda _, n=name: parent.switch_frame(n))
            else:
                btn.setProperty("role", "destructive")
                btn.clicked.connect(parent.close)
            lay.addWidget(btn)

    def _capture_aspect_once(self, _):
        fr = self.movie.frameRect()
        if fr.height() > 0:
            self._aspect = fr.width() / fr.height()
            self.movie.frameChanged.disconnect(self._capture_aspect_once)
            QTimer.singleShot(0, self._rescale_movie)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._rescale_movie()

    def _available_width(self) -> int:
        lay = self.layout()
        if lay is None:
            return self.width()
        m = lay.contentsMargins()
        return self.width() - (m.left() + m.right())

    def _rescale_movie(self):
        if not self.gif_lbl.isVisible() or self._aspect == 0:
            return
        w = self.gif_lbl.width()
        if w == 0:
            m = self.layout().contentsMargins()
            w = self.width() - (m.left() + m.right())
        h = int(w / self._aspect)
        self.gif_lbl.setFixedSize(w, h)
        self.movie.setScaledSize(QSize(w, h))

    def update_gif(self, show: bool):
        self.gif_lbl.setVisible(show)
        if show:
            if self.movie.state() != QMovie.Running:
                self.movie.start()
            QTimer.singleShot(0, self._rescale_movie)
        else:
            self.movie.stop()
            
        
class NurseManagementScreen(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.nm     = NurseManager(DB_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 24)
        layout.setSpacing(16)

        title = QLabel("Manage Nurses")
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(8)

        # Nurse list
        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 18))
        self.list.setSpacing(6)
        self.list.setStyleSheet("QListWidget { padding:6px; } QListWidget::item { height:26px; }")
        layout.addWidget(self.list, 1)

        # Buttons grid
        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
        self._btn_add    = QPushButton("Add Nurse")
        self._btn_remove = QPushButton("Remove Nurse"); self._btn_remove.setProperty("role", "destructive")
        self._btn_edit   = QPushButton("Edit Unavail");  self._btn_edit.setProperty("role", "special")
        self._btn_status = QPushButton("Set Status")
        for btn in (self._btn_add, self._btn_remove, self._btn_edit, self._btn_status):
            btn.setMinimumHeight(48)
            grid.addWidget(btn, 0 if btn in (self._btn_add, self._btn_remove) else 1,
                                0 if btn in (self._btn_add, self._btn_edit) else 1)
        layout.addLayout(grid)

        back = QPushButton("Back"); back.setMinimumHeight(48)
        back.setProperty("role", "special")
        back.clicked.connect(lambda: parent.switch_frame("main"))
        layout.addWidget(back)

        # connect
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove.clicked    .connect(self._on_remove)
        self._btn_edit.clicked      .connect(self._on_edit_unavail)
        self._btn_status.clicked    .connect(self._on_set_status)

        self.refresh()

    # ─────────────────── list maintenance ────────────────────
    def refresh(self):
        self.list.clear()
        for name in self.nm.get_nurses():
            prn  = self.nm.get_prn_status(name)
            late = self.nm.get_late_shift_status(name)
            label = name
            if prn:  label += " [PRN]"
            if late: label += " [Late]"
            self.list.addItem(label)

    # ─────────────────── Add Nurse (non-blocking) ─────────────
    class AddNurseDialog(ToolDialog):
        """Fixed version of the Add Nurse dialog"""
        def __init__(self, parent, save_cb):
            super().__init__(parent, "Add Nurse")
            self._save_cb = save_cb  # FIX: Store the callback!
            
            from PySide6.QtWidgets import QVBoxLayout, QLineEdit, QDialogButtonBox
            
            layout = QVBoxLayout()
            layout.setContentsMargins(24, 24, 24, 24)
            
            self.edit = QLineEdit()
            self.edit.setPlaceholderText("Enter nurse name...")
            layout.addWidget(self.edit)
            
            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._on_save)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            
            self.setLayout(layout)
            
            # Focus on the input field
            self.edit.setFocus()
        
        def _on_save(self):
            name = self.edit.text().strip()
            if name:
                self._save_cb(name)
                self.accept()
            else:
                show_warning(self, "Error", "Please enter a nurse name.")

    def _on_add(self):
        # Use the fixed AddNurseDialog class instead of the inner class
        dlg = self.AddNurseDialog(self, save_cb=lambda n: (self.nm.add_nurse(n), self.refresh()))
        dlg.open()

    # ─────────────────── Remove Nurse ─────────────────────────
    def _on_remove(self):
        item = self.list.currentItem()
        if not item:
            return
        name = item.text().split(" [")[0]
        confirm(self, "Confirm", f"Remove {name}?",
                yes_cb=lambda: (self.nm.remove_nurse(name), self.refresh()))

    def _on_edit_unavail(self):
        item = self.list.currentItem()
        if not item:
            return
        name   = item.text().split(" [")[0]
        dates  = {d.strftime("%Y-%m-%d")
                  for d in self.nm.get_unavailable_dates(name)}
    
        accent = self.parent.settings.get("accent_color")
        theme  = self.parent.settings.get("theme")
    
        dlg = ToolDialog(self.parent, title=f"Unavailable: {name}")
        v   = QVBoxLayout()
    
        picker = MultiDatePicker(dates, accent=accent, theme=theme)
        v.addWidget(picker)
    
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(lambda: (
            self.nm.update_unavailable_dates(name, picker.selected),
            dlg.accept(), self.refresh()
        ))
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
    
        dlg.setLayout(v)
        dlg.open()
    
    # ------------------------------------------------------------------
    #  Set Status
    # ------------------------------------------------------------------
    def _on_set_status(self):
        item = self.list.currentItem()
        if not item:
            return
        name = item.text().split(" [")[0]
        prn  = self.nm.get_prn_status(name)
        late = self.nm.get_late_shift_status(name)
    
        dlg  = ToolDialog(self.parent, title=f"Status: {name}")
        form = QFormLayout()
        cb1  = QCheckBox("PRN");         cb1.setChecked(prn)
        cb2  = QCheckBox("Late Shift");  cb2.setChecked(late)
        form.addRow(cb1); form.addRow(cb2)
    
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(lambda: (
            self.nm.set_prn_status(name, cb1.isChecked()),
            self.nm.set_late_shift_status(name, cb2.isChecked()),
            dlg.accept(), self.refresh()
        ))
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        dlg.setLayout(form); dlg.open()        
        # ─────────────────── Set Status (non-blocking) ────────────

class PreScheduledScreen(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.ps     = PreScheduler(DB_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Pre-scheduled Assignments")
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Date", "Main", "Backup", "Note"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 100)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch)

        self.table.setFont(QFont("Roboto", 14))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        QScroller.grabGesture(self.table.viewport(), QScroller.TouchGesture)
        layout.addWidget(self.table, 1)

        # buttons
        btn_row = QHBoxLayout(); btn_row.setSpacing(12)
        self._btn_add = QPushButton("Add")
        self._btn_mod = QPushButton("Modify"); self._btn_mod.setProperty("role","special")
        self._btn_del = QPushButton("Remove"); self._btn_del.setProperty("role","destructive")
        for b in (self._btn_add, self._btn_mod, self._btn_del):
            b.setMinimumHeight(48)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        back = QPushButton("Back"); back.setProperty("role","special")
        back.setMinimumHeight(48)
        back.clicked.connect(lambda: parent.switch_frame("main"))
        layout.addWidget(back)

        # signals
        self._btn_add.clicked.connect(self._on_add)
        self._btn_mod.clicked.connect(self._on_modify)
        self._btn_del.clicked.connect(self._on_remove)

        self.refresh()

    def _nurse_combo(self, current: str | None):
        nm = NurseManager(DB_NAME)
        cb = QComboBox(); cb.setFont(QFont("Roboto", 14))
        cb.addItems([""] + nm.get_nurses())
        cb.setCurrentText(current or "")
        return cb

    def _assignment_dialog(self, iso_ds, main, bak, note, save_cb):
        accent = self.parent.settings.get("accent_color")
        theme  = self.parent.settings.get("theme")
    
        dlg = ToolDialog(self.parent, "Assignment")
        dlg.setFixedWidth(410)
    
        form = QFormLayout()
        dummy = QLineEdit(); dummy.setFixedSize(0, 0); form.addRow(dummy)
    
        if iso_ds:
            form.addRow("Date:", QLabel(iso_ds))
        else:
            picker = SingleDatePicker(accent=accent,
                                      initial=QDate.currentDate(),
                                      theme=theme)
            form.addRow("Date:", picker)
    
        cbm = self._nurse_combo(main); form.addRow("Main:",   cbm)
        cbb = self._nurse_combo(bak);  form.addRow("Backup:", cbb)
        le  = QLineEdit(note or "");   form.addRow("Note:",   le)
    
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        def _save():
            ds = iso_ds or picker.iso()
            save_cb(ds, cbm.currentText(), cbb.currentText(), le.text())
            dlg.accept()
        btns.accepted.connect(_save); btns.rejected.connect(dlg.reject)
        form.addRow(btns)
    
        dlg.setLayout(form)
        pr = self.parent.geometry(); dr = dlg.frameGeometry()
        dlg.move(pr.center().x()-dr.width()//2, pr.center().y()-dr.height()//2)
        dlg.open()
        
    def refresh(self):
        raw = self.ps.get_assignments()
        rows = []
        for ds, main, bak, note in raw:
            try:
                dt = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            rows.append((dt, main or "", bak or "", note or ""))
        rows.sort(key=lambda x: x[0])

        self.table.setRowCount(len(rows))
        for i, (dt, m, b, n) in enumerate(rows):
            for j, txt in enumerate((dt.strftime("%m-%d-%y"), m, b, n)):
                itm = QTableWidgetItem(txt)
                if j == 0:
                    itm.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, j, itm)

    def _on_add(self):
        self._assignment_dialog(
            None, None, None, None,
            save_cb=lambda ds, m, b, n: (
                self.ps.add_assignment(ds, m or None, b or None, n),
                self.refresh()
            )
        )

    def _on_modify(self):
        row = self.table.currentRow()
        if row < 0:
            show_warning(self, "No selection", "Select a row first")
            return
        disp = self.table.item(row, 0).text()
        dt   = datetime.strptime(disp, "%m-%d-%y").date()
        iso  = dt.isoformat()
        rec  = next((r for r in self.ps.get_assignments() if r[0] == iso), None)
        if not rec:
            show_warning(self, "Missing", "Could not locate that record")
            return
        _, m, b, n = rec
        self._assignment_dialog(
            iso, m, b, n,
            save_cb=lambda ds, mm, bb, nn: (
                self.ps.add_assignment(ds, mm or None, bb or None, nn),
                self.refresh()
            )
        )

    def _on_remove(self):
        row = self.table.currentRow()
        if row < 0:
            return
        disp = self.table.item(row, 0).text()
        iso  = datetime.strptime(disp, "%m-%d-%y").date().isoformat()
        confirm(
            self, "Confirm", f"Remove {disp}?",
            yes_cb=lambda: (self.ps.remove_assignment(iso), self.refresh())
        )


class AssignmentHistoryScreen(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.ah     = AssignmentHistory(DB_NAME)
        self.nm     = NurseManager(DB_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Assignment History")
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # table
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Date", "Main", "Backup"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 100)
        for c in (1, 2):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch)

        self.table.setFont(QFont("Roboto", 16))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        QScroller.grabGesture(self.table.viewport(), QScroller.TouchGesture)
        layout.addWidget(self.table, 1)

        # buttons
        btn_row = QHBoxLayout(); btn_row.setSpacing(12)
        self._btn_add = QPushButton("Add")
        self._btn_mod = QPushButton("Modify"); self._btn_mod.setProperty("role","special")
        self._btn_del = QPushButton("Remove"); self._btn_del.setProperty("role","destructive")
        for b in (self._btn_add, self._btn_mod, self._btn_del):
            b.setMinimumHeight(48)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        # --- NEW: Sync Button ---
        self.btn_sync = QPushButton("Sync with Weekend History")
        self.btn_sync.setMinimumHeight(40)
        self.btn_sync.setProperty("role", "special")
        self.btn_sync.clicked.connect(self._on_sync)
        layout.addWidget(self.btn_sync)

        back = QPushButton("Back"); back.setProperty("role","special")
        back.setMinimumHeight(48)
        back.clicked.connect(lambda: parent.switch_frame("main"))
        layout.addWidget(back)

        # signals
        self._btn_add.clicked.connect(self._on_add)
        self._btn_mod.clicked.connect(self._on_modify)
        self._btn_del.clicked.connect(self._on_remove)

        self.refresh()

    def _nurse_combo(self, current: str | None):
        self.nm.refresh_cache()
        cb = QComboBox(); cb.setFont(QFont("Roboto", 14))
        cb.addItems([""] + self.nm.get_nurses())
        cb.setCurrentText(current or "")
        return cb

    def _assignment_dialog(self, iso_ds, main, bak, save_cb):
        accent = self.parent.settings.get("accent_color")
        theme  = self.parent.settings.get("theme")
    
        dlg = ToolDialog(self.parent, "Assignment History")
        dlg.setFixedWidth(410)
    
        form  = QFormLayout()
        dummy = QLineEdit(); dummy.setFixedSize(0, 0); form.addRow(dummy)
    
        if iso_ds:
            form.addRow("Date:", QLabel(iso_ds))
        else:
            picker = SingleDatePicker(accent=accent,
                                      initial=QDate.currentDate(),
                                      theme=theme)
            form.addRow("Date:", picker)
    
        cbm = self._nurse_combo(main); form.addRow("Main:",   cbm)
        cbb = self._nurse_combo(bak);  form.addRow("Backup:", cbb)
    
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        def _save():
            ds = iso_ds or picker.iso()
            save_cb(ds, cbm.currentText(), cbb.currentText())
            dlg.accept()
        btns.accepted.connect(_save); btns.rejected.connect(dlg.reject)
        form.addRow(btns)
    
        dlg.setLayout(form)
        pr = self.parent.geometry(); dr = dlg.frameGeometry()
        dlg.move(pr.center().x()-dr.width()//2, pr.center().y()-dr.height()//2)
        dlg.open() 
        
    def refresh(self):
        self.nm.refresh_cache()
        raw = self.ah.get_history()
        rows = []
        for ds, m, b in raw:
            try:
                dt = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            rows.append((dt, m or "", b or ""))
        rows.sort(key=lambda x: x[0])

        self.table.setRowCount(len(rows))
        for i, (dt, m, b) in enumerate(rows):
            for j, txt in enumerate((dt.strftime("%m-%d-%y"), m, b)):
                itm = QTableWidgetItem(txt)
                if j == 0:
                    itm.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, j, itm)

    def _on_add(self):
        self._assignment_dialog(
            None, None, None,
            save_cb=lambda ds, m, b: (
                self.ah.update_history(ds, m or "", b or ""),
                self.nm.refresh_cache(),
                self.refresh()
            )
        )

    def _on_modify(self):
        row = self.table.currentRow()
        if row < 0:
            show_warning(self, "No selection", "Select a row first")
            return
        disp = self.table.item(row, 0).text()
        iso  = datetime.strptime(disp, "%m-%d-%y").date().isoformat()
        rec  = next((r for r in self.ah.get_history() if r[0] == iso), None)
        if not rec:
            show_warning(self, "Missing", "Could not locate that record")
            return
        _, m, b = rec
        self._assignment_dialog(
            iso, m, b,
            save_cb=lambda ds, mm, bb: (
                self.ah.update_history(ds, mm or "", bb or ""),
                self.nm.refresh_cache(),
                self.refresh()
            )
        )

    def _on_remove(self):
        row = self.table.currentRow()
        if row < 0:
            return
        disp = self.table.item(row, 0).text()
        iso  = datetime.strptime(disp, "%m-%d-%y").date().isoformat()
        confirm(
            self, "Confirm", f"Remove {disp}?",
            yes_cb=lambda: (
                self.ah.delete_record(iso),
                self.nm.refresh_cache(),
                self.refresh()
            )
        )

    # --- NEW: Sync Button Handler ---
    def _on_sync(self):
        try:
            self.parent.backend._handle_sync_assignment_history_with_weekend()
            show_info(self, "Sync", "Sync complete.")
        except Exception as e:
            show_warning(self, "Sync Error", str(e))
        self.refresh()
        
        
class ViewAllUnavailableScreen(QWidget):

    _SPAN_RE = re.compile(r'color:\s*#[0-9A-Fa-f]{6}')

    # ───────────────────────────  ctor  ────────────────────────────
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        try:
            self.nm = NurseManager(DB_NAME)
        except Exception as e:
            show_error(self, "Database Error", str(e))
            self.nm = None

        # ui skeleton ------------------------------------------------------
        main = QVBoxLayout(self)
        main.setContentsMargins(24, 24, 24, 24)
        main.setSpacing(16)

        self.search = QLineEdit(placeholderText="Search by nurse name…",
                                font=QFont("Roboto", 17))
        main.addWidget(self.search)

        self.list = QListWidget(
            verticalScrollMode=QAbstractItemView.ScrollPerPixel,
            horizontalScrollBarPolicy=Qt.ScrollBarAlwaysOff,
            spacing=6, frameShape=QFrame.NoFrame)
        main.addWidget(self.list, 1)

        QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)

        self.edit_btn = QPushButton("Edit Unavailable", minimumHeight=48,
                                    font=QFont("Roboto", 16))
        main.addWidget(self.edit_btn)

        back = QPushButton("Back", minimumHeight=48)
        back.setProperty("role", "special")
        back.setFont(QFont("Roboto", 16, QFont.Bold))
        back.clicked.connect(lambda: parent.switch_frame("main"))
        main.addWidget(back)

        # signals ----------------------------------------------------------
        self.search.textChanged.connect(self._reload_list)
        self.list.currentItemChanged.connect(self._on_select_change)
        self.edit_btn.clicked.connect(self._on_edit)

        # data -> first populate ------------------------------------------
        self._all_data: list[tuple[str, list[str]]] = []
        self._load_all()
        self._apply_theme()

    # ─────────────── theme palette helper ────────────────
    @property
    def _colours(self):
        theme  = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        if theme == "dark":
            bg, edge = "#2D3238", shade_color("#2D3238", 1.15)
            act_bg, act_edge = accent, shade_color(accent, .80)
            month_idle, month_sel = accent, "#FFFFFF"
        elif theme == "light":
            bg, edge = "#FFFFFF", "#E1DDD6"
            act_bg, act_edge = accent, shade_color(accent, .80)
            month_idle, month_sel = accent, "#FFFFFF"
        else:                           # pink
            bg, edge = "#FFE6E6", "#F4C2C2"
            act_bg, act_edge = "#FFBFD2", "#E88AA5"
            month_idle = month_sel = "#AA5577"

        return bg, edge, act_bg, act_edge, month_idle, month_sel

    # ─────────────── card factory ────────────────────────
    def _build_card(self, name: str, iso_dates: list[str]) -> QWidget:
        bg, edge, *_ , month_idle, _ = self._colours

        html = [f"<b>{name}</b>"]
        if iso_dates:
            groups = defaultdict(list)
            for iso in iso_dates:
                y, m, d = iso.split("-")
                groups[(int(y), int(m))].append(int(d))
            for (yr, mo), days in sorted(groups.items()):
                span = (f'<span style="font-weight:600;color:{month_idle};">'
                        f'{calendar.month_abbr[mo]} {yr}:</span>')
                for i in range(0, len(days := sorted(days)), 8):
                    chunk = ", ".join(map(str, days[i:i+8]))
                    html.append(f"{span if i == 0 else '&nbsp;&nbsp;'} {chunk}")
        else:
            html.append("(no unavailable dates)")

        card = QWidget()
        card.setStyleSheet(self._card_css(bg, edge))
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lbl = QLabel("<br>".join(html), wordWrap=True,
                     textFormat=Qt.RichText, font=QFont("Roboto", 17))
        lay.addWidget(lbl)
        return card

    @staticmethod
    def _card_css(bg, edge):
        return f"background:{bg};border:1px solid {edge};border-radius:8px;"

    # helper to change month colour ---------------------------------------
    @classmethod
    def _tint_month(cls, card: QWidget, colour: str):
        lbl = card.findChild(QLabel)
        if lbl:
            lbl.setText(cls._SPAN_RE.sub(f"color:{colour}", lbl.text()))

    # ─────────────── list population ────────────────────
    def _load_all(self):
        if not self.nm:
            return
        self._all_data = []
        for name, info in self.nm.nurses.items():
            if self.nm.is_prn_nurse(name):
                continue
            iso = sorted(d.strftime("%Y-%m-%d") for d in info["unavailable_dates"])
            self._all_data.append((name, iso))
        self._reload_list()

    def _reload_list(self):
        term = self.search.text().lower().strip()
        self.list.blockSignals(True)
        self.list.clear()

        for name, iso_list in self._all_data:
            if term and term not in name.lower():
                continue
            card = self._build_card(name, iso_list)

            itm = QListWidgetItem()
            itm.setData(Qt.UserRole, name)
            self.list.addItem(itm)
            self.list.setItemWidget(itm, card)

        self.list.blockSignals(False)
        self.edit_btn.setEnabled(False)

        QTimer.singleShot(0, self._fix_item_sizes)

    # ─────────────── dynamic row-size fixer ─────────────
    def _fix_item_sizes(self):
        vw = self.list.viewport().width()           # current usable width
        for i in range(self.list.count()):
            itm  = self.list.item(i)
            card = self.list.itemWidget(itm)
            if not card:
                continue
            card.setFixedWidth(vw)                  # enforce exact width
            card.layout().activate()               # recalc wrapping
            card.adjustSize()
            itm.setSizeHint(card.sizeHint())

    # also call it on every list resize ------------------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fix_item_sizes)

    # ─────────────── selection highlight ────────────────
    def _on_select_change(self, cur, prev):
        bg, edge, act_bg, act_ed, month_idle, month_sel = self._colours
        if prev:
            pc = self.list.itemWidget(prev)
            pc.setStyleSheet(self._card_css(bg, edge))
            self._tint_month(pc, month_idle)
        if cur:
            cc = self.list.itemWidget(cur)
            cc.setStyleSheet(self._card_css(act_bg, act_ed))
            self._tint_month(cc, month_sel)
        self.edit_btn.setEnabled(cur is not None and self.nm is not None)

    # ─────────────── theme refresh from App ─────────────
    def apply_theme_update(self):
        sel = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        self._apply_theme()
        self._reload_list()
        if sel:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole) == sel:
                    self.list.setCurrentRow(i)
                    break

    def _apply_theme(self):
        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        if theme == "dark":
            bg, txt, sel_bg, sel_txt = "#252A32", "#E8EAF0", accent, "#FFFFFF"
        elif theme == "light":
            bg, txt, sel_bg, sel_txt = "#FFFFFF", "#2C2A27", accent, "#FFFFFF"
        else:
            bg, txt, sel_bg, sel_txt = "#F7D7DF", "#4A4A4A", "#E75480", "#FFFFFF"

        self.list.setStyleSheet(f"""
            QListWidget {{
                background:{bg};
                color:{txt};
                border:none;
            }}
            QListWidget::item {{
                padding:6px 8px; border-radius:8px;
            }}
            QListWidget::item:selected {{
                background:{sel_bg};
                color:{sel_txt};
            }}""")

    # ─────────────── edit handler (keep yours) ──────────
    def _on_edit(self):
        if not self.nm:
            return
        itm = self.list.currentItem()
        if not itm:
            return
        name = itm.data(Qt.UserRole)
        try:
            existing = {d.strftime("%Y-%m-%d")
                        for d in self.nm.get_unavailable_dates(name)}
        except Exception as e:
            show_warning(self, "Database Error", str(e)); return
    
        accent = self.parent.settings.get("accent_color")
        theme  = self.parent.settings.get("theme")
    
        dlg = ToolDialog(self.parent, f"Unavailable: {name}")
        v   = QVBoxLayout()
        picker = MultiDatePicker(existing, accent=accent, theme=theme)
        v.addWidget(picker)
    
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        def _save():
            try:
                self.nm.update_unavailable_dates(name, picker.selected)
                dlg.accept(); self._load_all()
            except Exception as e:
                show_error(dlg, "Database Error", str(e))
        btns.accepted.connect(_save); btns.rejected.connect(dlg.reject)
        v.addWidget(btns); dlg.setLayout(v); dlg.open() 
        
# ──────────────────────────────────────────────────────────────────────
#  Worker threads (unchanged from our earlier rewrite)
# ──────────────────────────────────────────────────────────────────────
from concurrent.futures import ThreadPoolExecutor

class ScheduleProgressWorker(QThread):
    progress = Signal(int, int)
    finished = Signal(object, object, object)
    error    = Signal(str)

    def __init__(self, start_date, end_date, *, allow_rotation_violations=False,
                 nurses_allowed_rotation_violation=None, settings=None):
        super().__init__()
        self._start = start_date
        self._end   = end_date
        self.allow_rotation_violations = allow_rotation_violations
        self.nurses_allowed_rotation_violation = nurses_allowed_rotation_violation or []
        try:
            if hasattr(settings, "all"):
                self.settings = settings.all()
            elif isinstance(settings, dict):
                self.settings = dict(settings)
            else:
                self.settings = {}
        except Exception:
            self.settings = {}
    
        # --- NEW: normalize legacy midweek toggles into the master flag
        try:
            if "allow_one_day_weekday_gap" not in self.settings:
                a = bool(self.settings.get("allow_midweek_pair_backup_only", False))
                b = bool(self.settings.get("allow_midweek_pair_mixed", False))
                self.settings["allow_one_day_weekday_gap"] = a or b
        except Exception:
            pass

    def run(self):
        try:
            apply_backend_debug_preferences(self.settings)
            nm = NurseManager(DB_NAME)
            wh = WeekendHistory(DB_NAME)
            ps = PreScheduler(DB_NAME)

            from NCSSQL55 import build_scheduler_from_settings
            sched = build_scheduler_from_settings(self._start, self._end, nm, wh, ps, self.settings)

            sched.set_allow_rotation_violations(self.allow_rotation_violations)
            sched.set_nurses_allowed_rotation_violation(self.nurses_allowed_rotation_violation or [])

            # Weekend variants only (never None here)
            variants = sched.generate_all_weekend_variants(
                allow_rotation_violations=self.allow_rotation_violations
            )
            if not variants:
                self.finished.emit([], sched, wh)
                return

            total = len(variants)
            candidate_schedules = []

            # Use threads on Android to avoid GL/fork issues; processes elsewhere
            override_env = os.environ.get("NSCHED_FORCE_THREAD_POOL")
            override = _coerce_env_flag(override_env)
            detected_android = is_android_platform()
            use_threads = detected_android if override is None else override
            maxw = min(4, os.cpu_count() or 1, total) if use_threads else min(8, os.cpu_count() or 1, total)
            Executor = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
            print(
                "[progress-worker] using"
                f" {'ThreadPoolExecutor' if use_threads else 'ProcessPoolExecutor'}"
                f" (android={detected_android}, override={override_env!r})"
            )

            try:
                with Executor(max_workers=maxw) as pool:
                    futures = [pool.submit(_evaluate_variant_worker, (i, v)) for i, v in enumerate(variants)]
                    for done, fut in enumerate(as_completed(futures), start=1):
                        try:
                            res = fut.result()
                            if res is not None:
                                candidate_schedules.append(res)
                        except Exception as ex:
                            # Keep going; we’ll do a serial fallback if everything failed
                            pass
                        self.progress.emit(done, total)
            except Exception:
                # Pool itself failed → serial path
                candidate_schedules.clear()

            # If nothing came back (exceptions or strict filters in older worker), build weekend-only candidates
            if not candidate_schedules:
                for i, var in enumerate(variants):
                    try:
                        df = var.state.schedule.copy()
                        counts = {}
                        for n in getattr(var, "nurses", []):
                            m = int((df["main"] == n).sum()) if "main" in df.columns else 0
                            b = int((df["backup"] == n).sum()) if "backup" in df.columns else 0
                            counts[n] = {"main": m, "backup": b, "total": m + b}
                        mains = list(counts[n]["main"] for n in counts) or [0]
                        backs = list(counts[n]["backup"] for n in counts) or [0]
                        stats = {
                            "gaps": int(df[["main", "backup"]].isna().sum().sum()) if not df.empty else 0,
                            "balance_main": int(max(mains) - min(mains)) if len(mains) > 1 else 0,
                            "balance_backup": int(max(backs) - min(backs)) if len(backs) > 1 else 0,
                            "rotation_rep": int(getattr(var.state, "rotation_repeats", 0)),
                        }
                        candidate_schedules.append((i, stats, counts, df))
                    except Exception:
                        # If even this fails for a variant, just skip it
                        pass

            if not candidate_schedules:
                # Truly nothing usable
                self.finished.emit([], sched, wh)
                return

            # Rank and return top-5
            try:
                sched._score_and_rank_variants(candidate_schedules)
            except Exception as ex:
                # If ranking fails, still show raw candidates
                pass

            if DEBUG_SAVE_VARIANTS:
                try:
                    sched._debug_variant_dump = _prepare_variant_debug_payload(
                        candidate_schedules,
                        sched,
                        wh,
                        self._start,
                    )
                except Exception:
                    pass

            self.finished.emit(candidate_schedules[:5], sched, wh)

        except Exception:
            self.error.emit(traceback.format_exc())
                
            
# ───────────────────────────────────────────────────────────────────
class ScheduleGenerationScreen(QWidget):
    """
    Lets the user choose start / end dates (matching SingleDatePicker look),
    then runs schedule generation in a background thread, with per-nurse rotation violation control.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(12)

        accent = parent.settings.get("accent_color")
        theme  = parent.settings.get("theme")

        # start / end pickers -------------------------------------------------
        for label_txt, attr in [("Start Date", "_start_cal"),
                                ("End Date",   "_end_cal")]:
            lbl = QLabel(label_txt, alignment=Qt.AlignCenter)
            lbl.setFont(UiStyle.TITLE_FONT); lay.addWidget(lbl)

            picker = SingleDatePicker(accent=accent,
                                      initial=QDate.currentDate(),
                                      theme=theme)
            lay.addWidget(picker)
            setattr(self, attr, picker)

        gen_btn = QPushButton("Generate Schedule")
        gen_btn.setFont(QFont("Roboto", 16))
        gen_btn.setMinimumHeight(48)
        gen_btn.clicked.connect(self._on_generate)
        lay.addWidget(gen_btn)

        back = QPushButton("Back", minimumHeight=48)
        back.setProperty("role", "special")
        back.setFont(QFont("Roboto", 16))
        back.clicked.connect(lambda: parent.switch_frame("main"))
        lay.addWidget(back)

        # placeholders
        self.wh = self.ah = self.backup = None
        self._progress = None
        self.worker    = None
        self._variant_dialog = None

    def _on_generate(self):
        s_iso = self._start_cal.iso()
        e_iso = self._end_cal.iso()
        start = datetime.strptime(s_iso, "%Y-%m-%d").date()
        end   = datetime.strptime(e_iso, "%Y-%m-%d").date()
        if end < start:
            show_warning(self, "Error", "End date is before start date")
            return
    
        # backup histories ---------------------------------------------------
        self.wh, self.ah = WeekendHistory(DB_NAME), AssignmentHistory(DB_NAME)
        self.backup      = self.wh.backup()
    
        theme  = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")
    
        # FIXED: Pass the backend object instead of separate nurses and violation_counts
        dlg = RotationViolationDialog(self, self.parent.backend, accent=accent, theme=theme)
    
        def after_dialog(accepted):
            if not accepted:
                return
            allow, nurses_allowed = dlg.get_values()
    
            # Now show progress dialog and launch worker
            if self._progress:
                self._progress.cancel()
            self._progress = QProgressDialog("Preparing…", None, 0, 100, self)
            self._progress.setWindowTitle("Generating Schedules")
            self._progress.setWindowModality(Qt.WindowModal)
            self._progress.setCancelButton(None)
            self._progress.setMinimumDuration(0)
            self._progress.setValue(0)
            self._progress.setFixedSize(320, 120)
            if theme == "dark":
                dlg_bg   = "#2D3238"; text = "#E8EAF0"; bar_bg = "#3A404B"
            elif theme == "light":
                dlg_bg   = "#F8F6F3"; text = "#2C2A27"; bar_bg = "#FFFFFF"
            else:  # pink
                dlg_bg   = "#F28AAC"; text = "#FFFFFF"; bar_bg = "#FFFFFF"
            self._progress.setStyleSheet(f"""
                QProgressDialog {{
                    background-color:{dlg_bg};
                    border:2px solid {accent};
                    border-radius:16px;
                    padding:10px; font-size:16px; color:{text};
                }}
                QProgressDialog QLabel {{ color:{text}; }}
                QProgressBar {{
                    height:20px; border-radius:10px;
                    background:{bar_bg}; text-align:center;
                    border:1px solid {accent};
                }}
                QProgressBar::chunk {{
                    background:{accent}; border-radius:10px;
                }}
            """)
            self._progress.show()
    
            # 3. Launch worker with allow/nurses_allowed
            self.worker = ScheduleProgressWorker(
                start, end,
                allow_rotation_violations=allow,
                nurses_allowed_rotation_violation=nurses_allowed,
                settings=self.parent.settings
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.error.connect(self._on_worker_error)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.start()
    
        dlg.accepted.connect(lambda: after_dialog(True))
        dlg.rejected.connect(lambda: after_dialog(False))
        dlg.open()
        
    def _on_progress(self, done: int, total: int):
        pct = int(100 * done / total) if total else 0
        self._progress.setMaximum(100)
        self._progress.setValue(pct)
        self._progress.setLabelText(f"Processing {done}/{total}  ({pct}%)")

    def _on_worker_error(self, msg: str):
        self._progress.cancel()
        show_error(self, "Error", msg)
        if self.wh and self.backup:
            self.wh.restore(self.backup)

    def apply_theme_update(self) -> None:
        """Apply theme and accent to calendar pickers when theme changes."""
        theme     = self.parent.settings.get("theme")
        accent    = self.parent.settings.get("accent_color")

        for picker in self.findChildren(MultiDatePicker):
            picker.set_theme(theme, accent)
        for picker in self.findChildren(SingleDatePicker):
            picker.set_theme(theme, accent)

    def _on_worker_finished(self, variants, scheduler, wh):
        # hide progress bar
        if self._progress:
            self._progress.cancel()
    
        # Guard: no feasible candidates
        if not variants:
            show_info(
                self, "No feasible schedules",
                "No valid schedules were generated for this date range and constraints."
            )
            if self.wh and self.backup:
                self.wh.restore(self.backup)
            return
    
        # ALWAYS save HTML + PDFs to a timestamped folder in Documents.
        try:
            out_dir = _save_outputs_for_variants(variants, scheduler, top_n=min(5, len(variants)))
            show_info(
                self, "Schedules exported",
                f"Saved calendar HTML and PDFs to:\n{out_dir}\n\n"
                "Open them manually to review. You can still use the in-app review now."
            )
        except Exception as e:
            show_warning(self, "Export failed", f"Could not save schedules:\n{e}")
    
        # Proceed to the normal review dialog
        self._variant_dialog = VariantReviewDialog(
            self.parent,
            variants, wh, self.ah, self.backup
        )
        self._variant_dialog.rejected.connect(lambda: self.wh.restore(self.backup))
        self._variant_dialog.open() 
        
class WeekendHistoryCalendarScreen(QWidget):
    """Friday-centred calendar + manual sync button."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.wh = WeekendHistory(DB_NAME)
        self.nm = NurseManager(DB_NAME)

        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(10)

        title = QLabel("Weekend History", alignment=Qt.AlignCenter)
        title.setFont(QFont("Roboto", 22, QFont.Bold))
        main.addWidget(title)

        # --- Sync button (smaller, less dominant) ---
        sync_row = QHBoxLayout()
        sync_row.addStretch()
        self.btn_sync = QPushButton("Sync with Assignment History")
        self.btn_sync.setFixedHeight(36)
        self.btn_sync.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.btn_sync.setProperty("role", "special")
        self.btn_sync.clicked.connect(self._on_sync)
        sync_row.addWidget(self.btn_sync)
        sync_row.addStretch()
        main.addLayout(sync_row)

        # --- Month navigation ---
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.btn_prev = QPushButton()
        self.btn_prev.setFixedSize(36, 36)
        self.btn_prev.setText("◀")
        self.btn_prev.clicked.connect(lambda: self._shift_month(-1))
        nav.addWidget(self.btn_prev)

        self.lbl_month = QLabel()
        self.lbl_month.setFont(QFont("Roboto", 16, QFont.Bold))
        self.lbl_month.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.lbl_month, 1)

        self.btn_next = QPushButton()
        self.btn_next.setFixedSize(36, 36)
        self.btn_next.setText("▶")
        self.btn_next.clicked.connect(lambda: self._shift_month(+1))
        nav.addWidget(self.btn_next)
        main.addLayout(nav)

        # --- Friday list ---
        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 15))
        self.list.setSpacing(4)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.itemSelectionChanged.connect(self._on_select)
        QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)
        main.addWidget(self.list, 1)

        # --- Action buttons row ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_add    = QPushButton("Add")
        self._btn_add.setFixedHeight(36)
        self._btn_modify = QPushButton("Modify")
        self._btn_modify.setFixedHeight(36)
        self._btn_modify.setProperty("role", "special")
        self._btn_remove = QPushButton("Remove")
        self._btn_remove.setFixedHeight(36)
        self._btn_remove.setProperty("role", "destructive")
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setFixedHeight(36)
        for b in (self._btn_add, self._btn_modify, self._btn_remove, self._btn_cancel):
            btn_row.addWidget(b)
        main.addLayout(btn_row)

        # --- Back button (smaller, less separated) ---
        back_row = QHBoxLayout()
        back_row.addStretch()
        back = QPushButton("Back")
        back.setFixedHeight(36)
        back.setProperty("role", "special")
        back.clicked.connect(lambda: parent.switch_frame("main"))
        back_row.addWidget(back)
        back_row.addStretch()
        main.addLayout(back_row)

        # --- Connections ---
        self._btn_add.clicked.connect(self._on_add)
        self._btn_modify.clicked.connect(self._on_modify)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_cancel.clicked.connect(self._on_cancel)

        # --- Initial content / theming ---
        today = date.today()
        self.month, self.year = today.month, today.year
        self._reload()
        self._populate_list()
        self._update_ui()
        self._apply_theme()

    def _apply_theme(self):
        self.lbl_month.setText(f"{calendar.month_name[self.month]} {self.year}")

    def _shift_month(self, delta: int):
        m, y = self.month + delta, self.year
        if m == 0:  m, y = 12, y - 1
        if m == 13: m, y = 1,  y + 1
        self.month, self.year = m, y
        self._refresh_all()

    def _reload(self):
        self.assign = {d.date().isoformat(): (fsf or "", sfs or "")
                       for d, fsf, sfs in self.wh.get_assignments()}

    def _populate_list(self):
        self.list.clear()
        cal = calendar.Calendar()
        for wk in cal.monthdatescalendar(self.year, self.month):
            dt = wk[4]  # Friday
            if dt.month != self.month:
                continue
            iso = dt.isoformat()
            fsf, sfs = self.assign.get(iso, ("", ""))
            txt = dt.strftime("%a %b %d") + (
                f" — FSF: {fsf or '-'} | SFS: {sfs or '-'}" if (fsf or sfs) else ""
            )
            itm = QListWidgetItem(txt); itm.setData(Qt.UserRole, iso)
            self.list.addItem(itm)

    def _refresh_all(self):
        self._reload()
        self._populate_list()
        self._update_ui()
        self._apply_theme()

    def _update_ui(self):
        sel = self.list.currentItem() is not None
        has = False
        if sel:
            iso = self.list.currentItem().data(Qt.UserRole)
            fsf, sfs = self.assign.get(iso, ("", ""))
            has = bool(fsf and sfs)
        self._btn_add.setEnabled(sel and not has)
        self._btn_modify.setEnabled(sel and has)
        self._btn_remove.setEnabled(sel and has)
        self._btn_cancel.setEnabled(sel)

    def _on_cancel(self):
        self.list.clearSelection()
        self._update_ui()

    def _on_select(self):
        self._update_ui()

    def _nurse_combo(self, current: str | None):
        cb = QComboBox(); cb.setFont(QFont("Roboto", 14))
        cb.addItems([""] + self.nm.get_nurses())
        cb.setCurrentText(current or "")
        return cb

    def _assignment_dialog(self, iso_ds: str, fsf: str, sfs: str, save_cb):
        dlg = ToolDialog(self.parent, "Weekend Assignment")
        dlg.setFixedWidth(410)
        form  = QFormLayout()
        dummy = QLineEdit(); dummy.setFixedSize(0, 0); form.addRow(dummy)
        form.addRow("Date:", QLabel(iso_ds))
        cbm = self._nurse_combo(fsf); form.addRow("FSF:", cbm)
        cbb = self._nurse_combo(sfs); form.addRow("SFS:", cbb)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        def _save():
            if cbm.currentText() == cbb.currentText():
                show_warning(dlg, "Invalid", "FSF and SFS must be different.")
                return
            save_cb(cbm.currentText(), cbb.currentText())
            dlg.accept(); self._refresh_all()
        btns.accepted.connect(_save); btns.rejected.connect(lambda:(dlg.reject(), self._update_ui()))
        form.addRow(btns); dlg.setLayout(form)
        QTimer.singleShot(0, dummy.setFocus)
        pr = self.parent.geometry(); dr = dlg.frameGeometry()
        dlg.move(pr.center().x()-dr.width()//2, pr.center().y()-dr.height()//2)
        dlg.open()

    def _on_add(self):
        item = self.list.currentItem()
        if not item:
            show_warning(self, "No selection", "Select a date first")
            return
        iso = item.data(Qt.UserRole)
        self._assignment_dialog(
            iso, "", "",
            save_cb=lambda fsf, sfs: (
                self.wh.add_assignment(iso, fsf, sfs),
                self._refresh_all()
            )
        )

    def _on_modify(self):
        item = self.list.currentItem()
        if not item:
            show_warning(self, "No selection", "Select a date first")
            return
        iso = item.data(Qt.UserRole)
        fsf, sfs = self.assign.get(iso, ("", ""))
        self._assignment_dialog(
            iso, fsf, sfs,
            save_cb=lambda new_fsf, new_sfs: (
                self.wh.remove_assignment(iso),
                self.wh.add_assignment(iso, new_fsf, new_sfs),
                self._refresh_all()
            )
        )

    def _on_remove(self):
        item = self.list.currentItem()
        if not item:
            return
        iso = item.data(Qt.UserRole)
        confirm(
            self, "Confirm",
            f"Remove assignment for {iso}?",
            yes_cb=lambda: (
                self.wh.remove_assignment(iso),
                self._refresh_all()
            )
        )

    def _on_sync(self):
        try:
            self.parent.backend._handle_sync_assignment_history_with_weekend()
            show_info(self, "Sync", "Weekend / Assignment history are now synced.")
            self._refresh_all()
        except Exception as e:
            show_warning(self, "Sync Error", str(e))
                   

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

        self.backend = NurseSchedulerUI(DB_NAME)  # Your backend logic instance

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
            # NurseSchedulerUI instance kept in self.backend
            # It created self.settings = SharedSettings() at init time.
            # Replace it with a fresh snapshot.
            from NCSSQL55 import SharedSettings
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
                             
# ──────────────────────────────────────────────────────────────────────
#  Entry-point
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    UiStyle.apply(app)           # apply defaults before window creation
    win = App(); win.showMaximized()
    sys.exit(app.exec())
