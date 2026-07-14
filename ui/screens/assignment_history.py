"""Assignment history screen — view/edit historical assignments."""

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

from scheduler import AssignmentHistory, NurseManager

from ..config import DB_NAME
from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_info, show_warning
from ..style import UiStyle
from ..widgets.date_pickers import SingleDatePicker


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

        # Sync Button
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

    def _on_sync(self):
        try:
            self.parent.backend.sync_assignment_history_with_weekend()
            show_info(self, "Sync", "Sync complete.")
        except Exception as e:
            show_warning(self, "Sync Error", str(e))
        self.refresh()


__all__ = ["AssignmentHistoryScreen"]
