"""Variant review dialog — touch-friendly review of generated schedule variants."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScroller,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..messages import show_info
from ..services.variant_export import export_variants_calendar_html
from ..theme import themed_icon
from .tool_dialog import ToolDialog


class VariantReviewDialog(ToolDialog):
    """Touch-friendly review of schedule variants. Prev/Next arrows obey theme."""

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
        QTimer.singleShot(0, self._update_page)
        self.showMaximized()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        self.header = QLabel(alignment=Qt.AlignCenter, font=self._HEAD_FONT)
        outer.addWidget(self.header)

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

        self.schedule_tbl = _make_tbl(3, ["Date", "Main", "Backup"])
        hh = self.schedule_tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.schedule_tbl.setColumnWidth(0, 100)
        outer.addWidget(self.schedule_tbl, 3)

        self.counts_tbl = _make_tbl(4, ["Nurse", "Main", "Backup", "Total"])
        for c in range(4):
            self.counts_tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch)
        outer.addWidget(self.counts_tbl, 2)

        nav = QHBoxLayout()
        nav.setSpacing(16)
        nav.addStretch()
        theme = (
            self.parent().settings.get("theme") if hasattr(self.parent(), "settings") else "pink"
        )

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

        nav.addWidget(self.prev_btn)
        nav.addSpacing(20)
        nav.addWidget(self.next_btn)
        nav.addStretch()
        outer.addLayout(nav)

        act = QHBoxLayout()
        act.setSpacing(16)
        act.addStretch()
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")

        self.calendar_btn = QPushButton("Calendar View")
        self.calendar_btn.clicked.connect(
            lambda: export_variants_calendar_html(self.variants, max_variants=5)
        )

        act.addWidget(self.calendar_btn)
        act.addSpacing(12)
        act.addWidget(self.save_btn)
        act.addSpacing(20)
        act.addWidget(self.cancel_btn)
        act.addStretch()
        outer.addLayout(act)

        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn.clicked.connect(self.reject)

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
        print(
            f"[review] cur={self._cur} total={total} "
            f"rows={len(self._parse_variant(self.variants[self._cur])[3]) if total else 0}"
        )

        if total == 0:
            self.header.setText("No schedules to display.")
            self.schedule_tbl.setRowCount(0)
            self.counts_tbl.setRowCount(0)
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.save_btn.setEnabled(False)
            return

        theme = (
            self.parent().settings.get("theme") if hasattr(self.parent(), "settings") else "pink"
        )
        fg = QColor("#E8EAF0") if theme == "dark" else QColor("#2C2A27")

        idx, st, counts, df = self._parse_variant(self.variants[self._cur])

        self.header.setText(
            f"Variant {self._cur + 1}/{total} • gaps {st.get('gaps', '—')}"
            f" • Δmain {st.get('balance_main', '—')}"
            f" • Δbackup {st.get('balance_backup', '—')}"
        )

        self.schedule_tbl.setRowCount(len(df))
        for r, (dt, row) in enumerate(df.iterrows()):
            values = (dt.date().isoformat(), row.get("main", "-"), row.get("backup", "-"))
            for c, val in enumerate(values):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)
                itm.setTextAlignment(Qt.AlignCenter if c == 0 else Qt.AlignVCenter | Qt.AlignLeft)
                itm.setForeground(fg)
                self.schedule_tbl.setItem(r, c, itm)

        nurses = sorted(counts)
        self.counts_tbl.setRowCount(len(nurses))
        for r, n in enumerate(nurses):
            m = counts[n]["main"]
            b = counts[n]["backup"]
            t = counts[n]["total"]
            for c, val in enumerate((n, m, b, t)):
                itm = QTableWidgetItem(str(val))
                itm.setFlags(Qt.ItemIsEnabled)
                itm.setTextAlignment(Qt.AlignCenter)
                itm.setForeground(fg)
                self.counts_tbl.setItem(r, c, itm)

        head_h = self.counts_tbl.horizontalHeader().height()
        visible = min(len(nurses), 9)
        self.counts_tbl.setFixedHeight(head_h + visible * self._ROW_H)

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
        for dt, row in df.iterrows():
            main, backup = row.get("main"), row.get("backup")
            if dt.weekday() == 4 and main and backup and main != backup:
                self.wh.modify_assignment(dt.isoformat(), main, backup)
            self.ah.update_history(dt.isoformat(), main, backup)

        show_info(self, "Saved", "Schedule and history have been updated.")
        self.accept()

    def apply_theme_update(self):
        """Refresh navigation button icons when theme changes."""
        theme = (
            self.parent().settings.get("theme") if hasattr(self.parent(), "settings") else "pink"
        )
        self.prev_btn.setIcon(themed_icon("arrowL.png", theme))
        self.next_btn.setIcon(themed_icon("arrowR.png", theme))


__all__ = ["VariantReviewDialog"]
