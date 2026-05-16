"""Pre-scheduled assignments screen."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScroller,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager, PreScheduler

from ..dialogs.tool_dialog import ToolDialog
from ..legacy import DB_NAME, UiStyle, confirm, show_warning
from ..widgets.date_pickers import SingleDatePicker


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


__all__ = ["PreScheduledScreen"]
