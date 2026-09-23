"""Variant review dialog — touch-friendly review of generated schedule variants."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScroller,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from scheduler import apply_schedule

from ..messages import confirm, show_error, show_info
from ..services.variant_export import export_variants_calendar_html
from ..style import UiStyle
from ..widgets.common import add_shortcut, set_role
from .tool_dialog import ToolDialog

logger = logging.getLogger(__name__)

# (stats key, label, tooltip) — shown for the variant on screen.
METRICS = [
    (
        "rotation_rep",
        "Rotation repeats",
        "Weekends where a nurse repeats the pattern (FSF/SFS) they worked last time "
        "instead of alternating. Options are ranked by this first. Ideally 0.",
    ),
    (
        "gaps",
        "Unfilled slots",
        "Main/Backup slots the scheduler could not fill. Options are ranked by this "
        "second. Ideally 0.",
    ),
    (
        "unfillable",
        "Impossible slots",
        "Unfilled slots that no nurse could legally take in this option, because of time "
        "off, spacing or the weekend rules. Fixing them needs a data or rule change.",
    ),
    (
        "weighted_score",
        "Score",
        "Combines the remaining metrics with the ranking weights from Settings, and "
        "orders options that tie on repeats and unfilled slots. Lower is better.",
    ),
    (
        "balance_main",
        "Main spread",
        "Difference between the most and fewest Main shifts any nurse gets. Lower is fairer.",
    ),
    (
        "balance_backup",
        "Backup spread",
        "Difference between the most and fewest Backup shifts any nurse gets. Lower is fairer.",
    ),
]


def _format_metric(key: str, value) -> str:
    if value is None:
        return "—"
    if key == "weighted_score":
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class VariantReviewDialog(ToolDialog):
    """Review the ranked schedule options, then apply one or close without applying."""

    _ROW_H = 26
    _HEAD_FONT = QFont("Roboto", 16, QFont.Bold)
    _CELL_FONT = QFont("Roboto", 12)

    def __init__(
        self,
        parent,
        variants,
        weekend_history,
        assignment_history,
        *,
        out_dir: str | None = None,
        export_error: str | None = None,
    ):
        super().__init__(parent, title="Review Schedules")
        self.variants = variants
        self.wh = weekend_history
        self.ah = assignment_history
        self._out_dir = out_dir
        self._export_error = export_error
        self._cur = 0
        self._build_ui()
        QTimer.singleShot(0, self._update_page)
        self.showMaximized()

    def _theme(self) -> str:
        parent = self.parent()
        return parent.settings.get("theme") if hasattr(parent, "settings") else "dark"

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        # Where the automatic export went, with a way to get there.
        if self._out_dir or self._export_error:
            banner = QFrame()
            banner.setObjectName("exportBanner")
            banner.setStyleSheet(
                "QFrame#exportBanner { border:1px solid palette(mid); border-radius:6px; }"
            )
            row = QHBoxLayout(banner)
            row.setContentsMargins(12, 8, 12, 8)
            if self._export_error:
                msg = QLabel(f"Could not save the PDF/HTML copies: {self._export_error}")
                msg.setProperty("role", "error")
            else:
                msg = QLabel(f"PDF and HTML copies of these options were saved to {self._out_dir}")
                msg.setProperty("role", "muted")
            msg.setWordWrap(True)
            msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(msg, 1)
            if self._out_dir and not self._export_error:
                open_btn = QPushButton("Open Folder")
                open_btn.setAutoDefault(False)
                open_btn.clicked.connect(self._open_out_dir)
                row.addWidget(open_btn)
            outer.addWidget(banner)

        self.header = QLabel(alignment=Qt.AlignCenter, font=self._HEAD_FONT)
        outer.addWidget(self.header)

        # Metric strip: label + value per metric, each with an explanation.
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(24)
        metrics_row.addStretch(1)
        self._metric_values: dict[str, QLabel] = {}
        for key, label, tip in METRICS:
            cell = QVBoxLayout()
            cell.setSpacing(0)
            value = QLabel("—", alignment=Qt.AlignCenter)
            value.setFont(QFont("Roboto", 16, QFont.Bold))
            caption = QLabel(label, alignment=Qt.AlignCenter)
            caption.setProperty("role", "muted")
            for w in (value, caption):
                w.setToolTip(tip)
            cell.addWidget(value)
            cell.addWidget(caption)
            metrics_row.addLayout(cell)
            self._metric_values[key] = value
        metrics_row.addStretch(1)
        outer.addLayout(metrics_row)
        hint = QLabel("Hover a metric for what it means. Lower is better for all of them.")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignCenter)
        outer.addWidget(hint)

        def _make_tbl(cols: int, headers: list[str]) -> QTableWidget:
            tbl = QTableWidget(0, cols, self)
            tbl.setHorizontalHeaderLabels(headers)
            tbl.setFont(self._CELL_FONT)
            tbl.verticalHeader().setDefaultSectionSize(self._ROW_H)
            tbl.verticalHeader().setVisible(False)
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tbl.setSelectionMode(QAbstractItemView.NoSelection)
            tbl.setAlternatingRowColors(True)
            tbl.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            vp = tbl.viewport()
            vp.setAttribute(Qt.WA_AcceptTouchEvents, True)
            for g in (QScroller.TouchGesture, QScroller.LeftMouseButtonGesture):
                QScroller.grabGesture(vp, g)
            return tbl

        tables = QHBoxLayout()
        tables.setSpacing(16)
        self.schedule_tbl = _make_tbl(3, ["Date", "Main", "Backup"])
        hh = self.schedule_tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        tables.addWidget(self.schedule_tbl, 3)

        self.counts_tbl = _make_tbl(4, ["Nurse", "Main", "Backup", "Total"])
        ch = self.counts_tbl.horizontalHeader()
        ch.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            ch.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        tables.addWidget(self.counts_tbl, 2)
        outer.addLayout(tables, 1)

        nav = QHBoxLayout()
        nav.setSpacing(16)
        nav.addStretch()
        self.prev_btn = QPushButton("‹  Previous option")
        self.prev_btn.setToolTip("Previous option (Left arrow)")
        self.next_btn = QPushButton("Next option  ›")
        self.next_btn.setToolTip("Next option (Right arrow)")
        for b in (self.prev_btn, self.next_btn):
            b.setMinimumWidth(160)
            b.setAutoDefault(False)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.next_btn)
        nav.addStretch()
        outer.addLayout(nav)

        act = QHBoxLayout()
        act.setSpacing(12)
        act.addStretch()
        self.calendar_btn = QPushButton("Open Calendar View")
        self.calendar_btn.setToolTip("Month-grid view of every option in your web browser")
        self.calendar_btn.setAutoDefault(False)
        self.calendar_btn.clicked.connect(
            lambda: export_variants_calendar_html(self.variants, max_variants=5)
        )
        self.cancel_btn = QPushButton("Close Without Applying")
        self.cancel_btn.setAutoDefault(False)
        self.save_btn = QPushButton("Apply This Schedule…")
        set_role(self.save_btn, "special")
        self.save_btn.setToolTip("Write the option on screen to assignment and weekend history")
        for b in (self.calendar_btn, self.cancel_btn, self.save_btn):
            b.setMinimumHeight(44)
            act.addWidget(b)
        act.addStretch()
        outer.addLayout(act)

        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn.clicked.connect(self.reject)
        add_shortcut(self, Qt.Key_Left, self._prev)
        add_shortcut(self, Qt.Key_Right, self._next)

    def _open_out_dir(self):
        if self._out_dir and os.path.isdir(self._out_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._out_dir))

    def _parse_variant(self, var):
        """Return idx, metrics dict, counts dict, df (counts built if missing)."""
        idx, metrics = var[0], var[1]
        if len(var) >= 4:
            counts, df = var[2], var[3]
        else:
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
        if total == 0:
            self.header.setText("No schedules to display.")
            self.schedule_tbl.setRowCount(0)
            self.counts_tbl.setRowCount(0)
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.save_btn.setEnabled(False)
            return

        colours = UiStyle.palette(self._theme())
        fg = QColor(colours["text"])
        weekend_fg = QColor(colours["weekend"])

        _idx, st, counts, df = self._parse_variant(self.variants[self._cur])

        rank = "best" if self._cur == 0 else f"#{self._cur + 1}"
        self.header.setText(f"Option {self._cur + 1} of {total} ({rank} ranked)")
        for key, _label, _tip in METRICS:
            self._metric_values[key].setText(_format_metric(key, st.get(key)))

        bold = QFont(self._CELL_FONT)
        bold.setBold(True)
        self.schedule_tbl.setRowCount(len(df))
        for r, (dt, row) in enumerate(df.iterrows()):
            is_weekend = dt.weekday() >= 4  # Fri–Sun belong to a weekend rotation
            values = (
                dt.strftime("%a %m/%d"),
                row.get("main") or "—",
                row.get("backup") or "—",
            )
            for c, val in enumerate(values):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)
                itm.setTextAlignment(Qt.AlignCenter if c == 0 else Qt.AlignVCenter | Qt.AlignLeft)
                itm.setForeground(weekend_fg if (c == 0 and is_weekend) else fg)
                if c == 0 and is_weekend:
                    itm.setFont(bold)
                self.schedule_tbl.setItem(r, c, itm)

        nurses = sorted(counts, key=str.casefold)
        self.counts_tbl.setRowCount(len(nurses))
        for r, n in enumerate(nurses):
            m = counts[n]["main"]
            b = counts[n]["backup"]
            t = counts[n]["total"]
            for c, val in enumerate((n, m, b, t)):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)
                itm.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft if c == 0 else Qt.AlignCenter)
                itm.setForeground(fg)
                self.counts_tbl.setItem(r, c, itm)

        self.prev_btn.setEnabled(self._cur > 0)
        self.next_btn.setEnabled(self._cur < total - 1)

    def _prev(self):
        if self._cur:
            self._cur -= 1
            self._update_page()

    def _next(self):
        if self._cur < len(self.variants) - 1:
            self._cur += 1
            self._update_page()

    def _save(self):
        _, _, _, df = self._parse_variant(self.variants[self._cur])
        if df.empty:
            return
        first = df.index.min().strftime("%b %d")
        last = df.index.max().strftime("%b %d, %Y")
        confirm(
            self,
            "Apply schedule",
            f"Apply option {self._cur + 1} for {first} – {last}?\n\n"
            f"This writes all {len(df)} days to assignment history and the weekend "
            "rotations to weekend history, replacing anything already recorded for "
            "those dates.",
            yes_cb=lambda: self._apply(df),
            yes_text="Apply",
        )

    def _apply(self, df):
        try:
            apply_schedule(self.wh.db_name, df)
        except Exception as exc:
            logger.exception("Applying option %d failed", self._cur + 1)
            show_error(
                self,
                "Schedule not applied",
                f"Option {self._cur + 1} could not be applied, so history was left unchanged.",
                details=str(exc),
            )
            return
        self.wh.reload()
        if self.ah is not None:
            self.ah.reload()

        show_info(
            self.parent(),
            "Schedule applied",
            f"Option {self._cur + 1} is now in assignment and weekend history.",
        )
        self.accept()

    def apply_theme_update(self):
        """Recolour the tables when the theme changes."""
        self._update_page()


__all__ = ["VariantReviewDialog", "METRICS"]
