"""Advanced weekend statistics screen — violation tracker + tools."""

from __future__ import annotations

import time

import pandas as pd
from PySide6.QtCore import QDate, QEvent, Qt, QTimer, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scheduler import WeekendPattern

from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_info
from ..widgets.date_pickers import SingleDatePicker
from ..worker_threads import RebuildViolationWorker


class AdvancedWeekendStatsScreen(QWidget):
    """Interactive, theme-aware statistics screen for weekend violations."""

    # ---- fonts & sizing ---------------------------------------------------
    BUTTON_HEIGHT     = 40
    BUTTON_MIN_WIDTH  = 150
    MIN_COL_WIDTH     = 80        # guarantees no header truncation

    BUTTON_FONT  = QFont("Roboto", 13, QFont.Bold)
    LABEL_FONT   = QFont("Roboto", 13)
    SECTION_FONT = QFont("Roboto", 16, QFont.Bold)
    TABLE_FONT   = QFont("Roboto", 12)
    HEADER_FONT  = QFont("Roboto", 13, QFont.Bold)
    TITLE_FONT   = QFont("Roboto", 20, QFont.Bold)

    HEADER_KEYS = [
        "Nurse",
        "V. Count",
        "Last\nViolation",
        "Viol.\nStreak",
        "Clean\nRuns",
        "Days\nSince",
    ]

    COLUMN_WEIGHTS = [1, 1, 1, 1, 1, 1]

    def __init__(self, parent):
        super().__init__(parent)
        self.parent         = parent
        self.backend        = parent.backend
        self._stats_date    = pd.Timestamp.today().strftime("%Y-%m-%d")
        self._current_nurse: str | None = None
        self._press_time    = None
        self._press_row     = None

        self._build_ui()
        self.show_stats()
        QTimer.singleShot(0, self._finalize_table_layout)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(18, 18, 18, 18)

        title = QLabel("Rotation Violation Stats", alignment=Qt.AlignCenter)
        title.setFont(self.TITLE_FONT)
        root.addWidget(title)

        date_row = QHBoxLayout()
        self.btn_change_date = self._make_button("Change Date", fixed_w=130)
        self.btn_change_date.clicked.connect(self._open_date_picker)
        date_row.addWidget(self.btn_change_date, alignment=Qt.AlignLeft)

        date_row.addStretch()

        self.lbl_stats_date = QLabel("", alignment=Qt.AlignRight)
        self.lbl_stats_date.setFont(QFont("Roboto", 14, QFont.Bold))
        date_row.addWidget(self.lbl_stats_date)
        root.addLayout(date_row)

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
        self.header_scroll.setMinimumWidth(0)

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
            lbl.setMinimumWidth(self.MIN_COL_WIDTH)
            lbl.mousePressEvent = lambda _, col=idx: self._sort_by_column(col)
            self.header_row.addWidget(lbl)
            self.header_labels.append(lbl)

        self.header_scroll.setWidget(self.header_container)
        root.addWidget(self.header_scroll)

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

        hhdr = self.table.horizontalHeader()
        hhdr.setSectionResizeMode(QHeaderView.Fixed)

        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.table.horizontalScrollBar().valueChanged.connect(
            self._sync_header_scroll
        )

        root.addWidget(self.table, 1)
        root.setStretchFactor(self.table, 1)

        self.lbl_selected_nurse = QLabel("Selected Nurse: None", alignment=Qt.AlignLeft)
        self.lbl_selected_nurse.setFont(QFont("Roboto", 15, QFont.Bold))
        root.addWidget(self.lbl_selected_nurse)

        root.addWidget(self._build_edit_group())

        self.btn_rebuild.clicked.connect(self.rebuild_history)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.installEventFilter(self)

        self.apply_theme_update()

    def _make_button(self, text: str, *, fixed_w: int | None = None) -> QPushButton:
        btn = QPushButton(text)
        btn.setFont(self.BUTTON_FONT)
        btn.setFixedHeight(self.BUTTON_HEIGHT)
        btn.setMinimumWidth(self.BUTTON_MIN_WIDTH)
        if fixed_w:
            btn.setFixedWidth(fixed_w)
        return btn

    def _build_edit_group(self) -> QGroupBox:
        edit_group = QGroupBox("Edit Selected Nurse")
        edit_group.setFont(self.SECTION_FONT)

        edit_grid = QGridLayout(edit_group)
        edit_grid.setHorizontalSpacing(10)
        edit_grid.setVerticalSpacing(6)
        edit_grid.setContentsMargins(14, 6, 14, 6)

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

        edit_grid.addItem(
            QSpacerItem(1, 6, QSizePolicy.Minimum, QSizePolicy.Fixed), 2, 0
        )

        self.btn_back = self._make_button("Back")
        self.btn_back.setFont(QFont("Roboto", 18, QFont.Bold))
        self.btn_back.clicked.connect(lambda: self.parent.switch_frame("main"))
        edit_grid.addWidget(self.btn_back, 3, 0, 1, 3)

        return edit_group

    def _finalize_table_layout(self) -> None:
        if len(self.header_labels) != len(self.HEADER_KEYS):
            return

        total_width = self.width() - 40
        total_weight = sum(self.COLUMN_WEIGHTS)

        base_widths = []
        for weight in self.COLUMN_WEIGHTS:
            width = int(total_width * weight / total_weight)
            base_widths.append(width)

        final_widths = []
        for i, base_width in enumerate(base_widths):
            if i < len(self.header_labels):
                text_metrics = self.header_labels[i].fontMetrics()
                text_width = text_metrics.boundingRect(self.HEADER_KEYS[i]).width() + 20
                min_needed = max(text_width, self.MIN_COL_WIDTH)
            else:
                min_needed = self.MIN_COL_WIDTH

            final_width = max(base_width, min_needed)
            final_widths.append(final_width)

        current_total = sum(final_widths)
        if current_total > total_width:
            scale = total_width / current_total
            final_widths = [int(w * scale) for w in final_widths]
            remainder = total_width - sum(final_widths)
            final_widths[-1] += remainder

        header_total = 0
        for i, width in enumerate(final_widths):
            self.table.setColumnWidth(i, width)

            if i < len(self.header_labels):
                self.header_labels[i].setFixedWidth(width)
                header_total += width

        self.header_container.setFixedWidth(header_total)
        self.header_container.setMinimumWidth(header_total)
        self.header_container.setMaximumWidth(header_total)

        self.header_scroll.widget().adjustSize()

        for r in range(self.table.rowCount()):
            self.table.setRowHeight(r, 22)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(50, self._finalize_table_layout)

    def _sync_header_scroll(self, value: int) -> None:
        self.header_scroll.horizontalScrollBar().setValue(value)

    def show_stats(self) -> None:
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

        header_css = (
            f"background:{header_bg};color:{header_fg};"
            f"font-weight:bold;font-size:13px;padding:4px;border:1px solid {grid};"
        )
        for lbl in self.header_labels:
            lbl.setStyleSheet(header_css)

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
        hdr = self.table.horizontalHeader()
        current = hdr.sortIndicatorSection()
        order   = hdr.sortIndicatorOrder()
        if current == col:
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

    def _on_table_cell_double_clicked(self, row, col):
        nurse = self.table.item(row, 0).text()
        QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

    def _on_table_context_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row >= 0:
            nurse = self.table.item(row, 0).text()
            QTimer.singleShot(0, lambda: self.show_nurse_details(nurse))

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

    def save_count(self):
        nurse = self._current_nurse
        if not nurse:
            show_info(self, "Select Nurse", "Choose a nurse first.")
            return
        try:
            self.backend.weekend_history.set_violation_count(nurse, self.spn_violation.value())
            show_info(self, "Saved", f"Violation count updated for {nurse}.")
            self.show_stats()
        except Exception as exc:
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
        except Exception as exc:
            show_info(self, "Error", str(exc))

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
        self._stats_date = picker.iso()
        dlg.accept()
        self.show_stats()


__all__ = ["AdvancedWeekendStatsScreen"]
