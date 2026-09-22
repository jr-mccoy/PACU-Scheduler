"""Rotation violation statistics screen — per-nurse violation tracker + tools."""

from __future__ import annotations

import time

import pandas as pd
from PySide6.QtCore import QDate, QEvent, Qt, QTimer, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressDialog,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scheduler import WeekendPattern

from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_error, show_info
from ..widgets.common import action_button, back_button, install_empty_state, screen_title
from ..widgets.date_pickers import SingleDatePicker
from ..worker_threads import RebuildViolationWorker

# The backend reports 999 for "never violated" in the clean-run and
# days-since columns.
NEVER = 999
SORT_KEY_ROLE = Qt.UserRole + 1

# (header, tooltip)
COLUMNS = [
    ("Nurse", ""),
    ("Violations", "Total weekends where the nurse repeated their previous pattern (FSF/SFS)"),
    ("Last violation", "Date of the most recent rotation violation"),
    ("Streak", "Consecutive most recent weekends that were violations"),
    ("Clean weekends", "Weekends worked since the last violation"),
    ("Days since", "Days between the last violation and the stats date"),
]


class _SortableItem(QTableWidgetItem):
    """Table item that sorts by a hidden key (numbers numerically, dates by date)."""

    def __lt__(self, other):
        mine, theirs = self.data(SORT_KEY_ROLE), other.data(SORT_KEY_ROLE)
        if mine is not None and theirs is not None:
            return mine < theirs
        return super().__lt__(other)


def _item(text: str, sort_key=None, *, center: bool = True) -> QTableWidgetItem:
    itm = _SortableItem(text)
    itm.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
    if sort_key is not None:
        itm.setData(SORT_KEY_ROLE, sort_key)
    if center:
        itm.setTextAlignment(Qt.AlignCenter)
    return itm


class AdvancedWeekendStatsScreen(QWidget):
    """Interactive, theme-aware statistics screen for weekend rotation violations."""

    TITLE = "Rotation Violation Stats"
    TABLE_FONT = QFont("Roboto", 13)

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.backend = parent.backend
        self._stats_date = pd.Timestamp.today().strftime("%Y-%m-%d")
        self._current_nurse: str | None = None
        self._press_time = None
        self._press_row = None
        self.worker = None
        self.progress = None

        self._build_ui()
        self.show_stats()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        root.addWidget(screen_title(self.TITLE))
        sub = QLabel(
            "A violation is a weekend where a nurse worked the same pattern as last time "
            "instead of alternating. Double-click a nurse for details."
        )
        sub.setProperty("role", "muted")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        root.addWidget(sub)

        bar = QHBoxLayout()
        self.lbl_stats_date = QLabel("")
        self.lbl_stats_date.setFont(QFont("Roboto", 14, QFont.Bold))
        bar.addWidget(self.lbl_stats_date)
        self.btn_change_date = action_button(
            "Change Date…", tooltip="Compute the stats as of another date"
        )
        bar.addWidget(self.btn_change_date)
        bar.addStretch()
        self.btn_rebuild = action_button(
            "Rebuild From Weekend History…",
            tooltip="Recalculate every count from the recorded weekends",
        )
        bar.addWidget(self.btn_rebuild)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for h, _ in COLUMNS])
        for c, (_h, tip) in enumerate(COLUMNS):
            if tip:
                self.table.horizontalHeaderItem(c).setToolTip(tip)
        self.table.setFont(self.TABLE_FONT)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSortIndicatorShown(True)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicator(0, Qt.AscendingOrder)
        install_empty_state(self.table, "No nurses on the roster yet.")
        root.addWidget(self.table, 1)

        root.addWidget(self._build_edit_group())
        root.addWidget(back_button(self, self.parent))

        self.btn_change_date.clicked.connect(self._open_date_picker)
        self.btn_rebuild.clicked.connect(self.rebuild_history)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.viewport().installEventFilter(self)

        self.apply_theme_update()

    def _build_edit_group(self) -> QGroupBox:
        self.edit_group = QGroupBox("Override for selected nurse")
        self.edit_group.setToolTip(
            "Manual overrides last until the next rebuild from weekend history."
        )

        edit_grid = QGridLayout(self.edit_group)
        edit_grid.setHorizontalSpacing(10)
        edit_grid.setVerticalSpacing(8)
        edit_grid.setContentsMargins(14, 10, 14, 10)

        self.lbl_selected_nurse = QLabel("Select a nurse in the table to edit.")
        self.lbl_selected_nurse.setProperty("role", "muted")
        edit_grid.addWidget(self.lbl_selected_nurse, 0, 0, 1, 3)

        edit_grid.addWidget(QLabel("Violation count:"), 1, 0, Qt.AlignRight)
        self.spn_violation = QSpinBox(self.edit_group)
        self.spn_violation.setRange(0, 99)
        self.spn_violation.setMinimumWidth(80)
        edit_grid.addWidget(self.spn_violation, 1, 1)
        self.btn_save_cnt = action_button("Save Count")
        self.btn_save_cnt.clicked.connect(self.save_count)
        edit_grid.addWidget(self.btn_save_cnt, 1, 2)

        edit_grid.addWidget(QLabel("Last pattern:"), 2, 0, Qt.AlignRight)
        self.cmb_pattern = QComboBox()
        self.cmb_pattern.addItem("FSF (Fri + Sun)", "FSF")
        self.cmb_pattern.addItem("SFS (Sat)", "SFS")
        self.cmb_pattern.setToolTip("The pattern the scheduler treats as this nurse's last one")
        edit_grid.addWidget(self.cmb_pattern, 2, 1)
        self.btn_save_pat = action_button("Save Pattern")
        self.btn_save_pat.clicked.connect(self.save_pattern)
        edit_grid.addWidget(self.btn_save_pat, 2, 2)
        edit_grid.setColumnStretch(2, 1)

        return self.edit_group

    # ─────────────────────────── data ───────────────────────────
    def on_show(self):
        self.show_stats()

    def show_stats(self) -> None:
        as_of = pd.to_datetime(self._stats_date)
        self.lbl_stats_date.setText(f"Stats as of {as_of:%a %b %d, %Y}")

        summary = self.backend.weekend_history.get_violation_summary(as_of=as_of)

        hdr = self.table.horizontalHeader()
        sort_col, sort_order = hdr.sortIndicatorSection(), hdr.sortIndicatorOrder()
        # Populating with sorting on reorders rows mid-insert; sort once at the end.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        rows = list(summary.itertuples(index=False))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            last = row.last_violation_date
            has_last = last is not None and not pd.isna(last)
            last_txt = pd.Timestamp(last).strftime("%b %d, %Y") if has_last else "Never"
            last_key = pd.Timestamp(last).value if has_last else -1

            clean = int(row.clean_run_weeks)
            since = int(row.days_since_last)
            cells = [
                _item(str(row.nurse), str(row.nurse).casefold(), center=False),
                _item(str(int(row.total_viol)), int(row.total_viol)),
                _item(last_txt, last_key),
                _item(str(int(row.consec_viol)), int(row.consec_viol)),
                _item("—" if clean >= NEVER else str(clean), clean),
                _item("—" if since >= NEVER else str(since), since),
            ]
            for c, itm in enumerate(cells):
                self.table.setItem(r, c, itm)

        self.table.setSortingEnabled(True)
        if 0 <= sort_col < len(COLUMNS):
            self.table.sortItems(sort_col, sort_order)
        self._restore_selection()
        self.refresh_edit_fields()

    def apply_theme_update(self) -> None:
        # Colours come from the application style sheet; nothing screen-specific.
        pass

    # ─────────────────────────── interaction ───────────────────────────
    def eventFilter(self, obj, event):
        # Long-press (touch screens) opens the details, like a double-click.
        if obj is self.table.viewport():
            if event.type() == QEvent.MouseButtonPress and event.buttons() & Qt.LeftButton:
                self._press_time = time.time()
                self._press_row = self.table.indexAt(event.pos()).row()
            elif event.type() == QEvent.MouseButtonRelease and self._press_time is not None:
                if time.time() - self._press_time > 0.7 and self._press_row >= 0:
                    nurse = self.table.item(self._press_row, 0).text()
                    QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))
                self._press_time, self._press_row = None, None
        return super().eventFilter(obj, event)

    def _on_table_cell_double_clicked(self, row, col):
        nurse = self.table.item(row, 0).text()
        QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

    def _on_table_context_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row >= 0:
            nurse = self.table.item(row, 0).text()
            QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

    def _on_table_selection(self):
        rows = self.table.selectionModel().selectedRows()
        self._current_nurse = self.table.item(rows[0].row(), 0).text() if rows else None
        self.refresh_edit_fields()

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

    def show_nurse_details(self, nurse: str):
        as_of = pd.to_datetime(self._stats_date)
        summary = self.backend.weekend_history.get_violation_summary(as_of=as_of)
        row = summary.loc[summary["nurse"] == nurse]
        if row.empty:
            return
        row = row.iloc[0]
        last = row.last_violation_date
        last_txt = "Never" if pd.isna(last) else pd.Timestamp(last).strftime("%a %b %d, %Y")
        clean = int(row.clean_run_weeks)
        since = int(row.days_since_last)
        msg = (
            f"Total violations: {int(row.total_viol)}\n"
            f"Last violation: {last_txt}\n"
            f"Current streak: {int(row.consec_viol)}\n"
            f"Clean weekends since: {'—' if clean >= NEVER else clean}\n"
            f"Days since last: {'—' if since >= NEVER else since}"
        )
        show_info(self, f"{nurse} — rotation", msg)

    def refresh_edit_fields(self):
        nurse = self._current_nurse
        enabled = nurse is not None
        for w in (self.spn_violation, self.cmb_pattern, self.btn_save_cnt, self.btn_save_pat):
            w.setEnabled(enabled)
        self.lbl_selected_nurse.setText(
            f"Editing {nurse}" if enabled else "Select a nurse in the table to edit."
        )
        if not enabled:
            self.spn_violation.setValue(0)
            self.cmb_pattern.setCurrentIndex(0)
            return

        cnts = self.backend.weekend_history.get_violation_counts()
        self.spn_violation.setValue(cnts.get(nurse, 0))
        lp = self.backend.weekend_history.get_last_pattern(nurse)
        idx = self.cmb_pattern.findData(lp.value if lp else "FSF")
        self.cmb_pattern.setCurrentIndex(max(idx, 0))

    def save_count(self):
        nurse = self._current_nurse
        if not nurse:
            return
        try:
            self.backend.weekend_history.set_violation_count(nurse, self.spn_violation.value())
        except Exception as exc:
            show_error(self, "Count not saved", f"Could not update {nurse}'s count.", str(exc))
            return
        show_info(self, "Saved", f"Violation count updated for {nurse}.")
        self.show_stats()

    def save_pattern(self):
        nurse = self._current_nurse
        if not nurse:
            return
        try:
            pat = WeekendPattern(self.cmb_pattern.currentData())
            self.backend.weekend_history.set_last_pattern(nurse, pat)
        except Exception as exc:
            show_error(self, "Pattern not saved", f"Could not update {nurse}'s pattern.", str(exc))
            return
        show_info(self, "Saved", f"Last pattern updated for {nurse}.")
        self.show_stats()

    def rebuild_history(self):
        def _start_rebuild():
            self.progress = QProgressDialog(
                "Rebuilding violation history…",
                None,
                0,
                0,
                self,
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
            "Rebuild violation history",
            "Recalculate every nurse's violation count, streak and last pattern from the "
            "recorded weekends?\n\nManual overrides made on this screen will be replaced.",
            yes_cb=_start_rebuild,
            yes_text="Rebuild",
            destructive=True,
        )

    def _on_rebuild_done(self, success: bool, msg: str):
        if self.progress:
            self.progress.cancel()
        self.worker = None
        if success:
            show_info(self, "Rebuilt", msg)
            self.show_stats()
        else:
            show_error(self, "Rebuild failed", "Violation history was not rebuilt.", msg)

    def _open_date_picker(self):
        dlg = ToolDialog(self, "Stats Date")
        dlg.resize(520, 560)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)

        picker = SingleDatePicker(
            accent=self.parent.settings.get("accent_color"),
            initial=QDate.fromString(self._stats_date, "yyyy-MM-dd"),
            theme=self.parent.settings.get("theme"),
            grid=bool(self.parent.settings.get("calendar_grid")),
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
        self._stats_date = picker.iso()
        dlg.accept()
        self.show_stats()


__all__ = ["AdvancedWeekendStatsScreen"]
