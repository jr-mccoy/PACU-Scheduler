"""Weekend history calendar screen — Friday-centred view + manual sync."""

from __future__ import annotations

import calendar
from datetime import date

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScroller,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager, WeekendHistory

from ..config import DB_NAME
from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_info, show_warning


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
            self.parent.backend.sync_assignment_history_with_weekend()
            show_info(self, "Sync", "Weekend / Assignment history are now synced.")
            self._refresh_all()
        except Exception as e:
            show_warning(self, "Sync Error", str(e))


__all__ = ["WeekendHistoryCalendarScreen"]
