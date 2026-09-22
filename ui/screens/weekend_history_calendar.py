"""Weekend history calendar screen — Friday-centred view + manual sync."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScroller,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager, WeekendHistory

from ..config import DB_NAME
from ..dialogs.assignment_dialog import nurse_combo
from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_warning
from ..widgets.common import (
    action_button,
    add_shortcut,
    back_button,
    nav_arrow_button,
    refresh_nav_arrow,
    screen_title,
)
from .assignment_history import SYNC_LABEL, SYNC_TOOLTIP, run_weekend_sync


def _weekend_label(friday: date, fsf: str, sfs: str) -> str:
    head = f"{friday:%a %b %d, %Y}"
    if not (fsf or sfs):
        return f"{head}     —  not recorded"
    return f"{head}     FSF: {fsf or '—'}     SFS: {sfs or '—'}"


class WeekendHistoryCalendarScreen(QWidget):
    """One row per weekend (by its Friday) for the month shown."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.wh = WeekendHistory(DB_NAME)
        self.nm = NurseManager(DB_NAME)
        theme = parent.settings.get("theme")

        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(12)

        main.addWidget(screen_title("Weekend History"))
        sub = QLabel(
            "Who worked each weekend: FSF covers Friday and Sunday, SFS covers Saturday. "
            "The scheduler alternates each nurse between the two."
        )
        sub.setProperty("role", "muted")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        main.addWidget(sub)

        # --- Month navigation ---
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.btn_prev = nav_arrow_button(prev=True, tooltip="Previous month (Page Up)", theme=theme)
        self.btn_prev.clicked.connect(lambda: self._shift_month(-1))
        nav.addWidget(self.btn_prev)

        self.lbl_month = QLabel()
        self.lbl_month.setFont(QFont("Roboto", 16, QFont.Bold))
        self.lbl_month.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.lbl_month, 1)

        self.btn_today = QPushButton("This Month")
        self.btn_today.clicked.connect(self._go_today)
        nav.addWidget(self.btn_today)

        self.btn_next = nav_arrow_button(prev=False, tooltip="Next month (Page Down)", theme=theme)
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
        self.list.itemDoubleClicked.connect(lambda _i: self._on_edit_or_add())
        QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)
        main.addWidget(self.list, 1)

        # --- Action buttons row ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self._btn_add = action_button(
            "Record…", "special", tooltip="Record who worked this weekend"
        )
        self._btn_modify = action_button("Edit…", tooltip="Change this weekend's nurses (Enter)")
        self._btn_remove = action_button(
            "Remove", "destructive", tooltip="Delete this weekend's record (Delete)"
        )
        self._btn_cancel = action_button("Clear Selection")
        for b in (self._btn_add, self._btn_modify, self._btn_remove, self._btn_cancel):
            btn_row.addWidget(b)
        main.addLayout(btn_row)

        self.btn_sync = action_button(SYNC_LABEL, tooltip=SYNC_TOOLTIP)
        self.btn_sync.clicked.connect(self._on_sync)
        main.addWidget(self.btn_sync)

        main.addWidget(back_button(self, parent))

        # --- Connections ---
        self._btn_add.clicked.connect(self._on_add)
        self._btn_modify.clicked.connect(self._on_modify)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_cancel.clicked.connect(self._on_cancel)
        add_shortcut(self.list, Qt.Key_Delete, self._on_remove)
        add_shortcut(self.list, Qt.Key_Return, self._on_edit_or_add)
        add_shortcut(self, Qt.Key_PageUp, lambda: self._shift_month(-1))
        add_shortcut(self, Qt.Key_PageDown, lambda: self._shift_month(+1))

        # --- Initial content / theming ---
        today = date.today()
        self.month, self.year = today.month, today.year
        self._refresh_all()

    def on_show(self):
        self.wh.reload()
        self.nm.refresh_cache()
        self._refresh_all()

    def apply_theme_update(self):
        theme = self.parent.settings.get("theme")
        refresh_nav_arrow(self.btn_prev, prev=True, theme=theme)
        refresh_nav_arrow(self.btn_next, prev=False, theme=theme)

    def _update_month_label(self):
        self.lbl_month.setText(f"{calendar.month_name[self.month]} {self.year}")

    def _shift_month(self, delta: int):
        m, y = self.month + delta, self.year
        if m == 0:
            m, y = 12, y - 1
        if m == 13:
            m, y = 1, y + 1
        self.month, self.year = m, y
        self._refresh_all()

    def _go_today(self):
        today = date.today()
        self.month, self.year = today.month, today.year
        self._refresh_all()

    def _reload(self):
        self.assign = {
            d.date().isoformat(): (fsf or "", sfs or "")
            for d, fsf, sfs in self.wh.get_assignments()
        }

    def _populate_list(self, select: str | None = None):
        self.list.clear()
        cal = calendar.Calendar()
        for wk in cal.monthdatescalendar(self.year, self.month):
            dt = wk[4]  # Friday
            if dt.month != self.month:
                continue
            iso = dt.isoformat()
            fsf, sfs = self.assign.get(iso, ("", ""))
            itm = QListWidgetItem(_weekend_label(dt, fsf, sfs))
            itm.setData(Qt.UserRole, iso)
            self.list.addItem(itm)
            if iso == select:
                self.list.setCurrentItem(itm)

    def _selected_iso(self) -> str | None:
        item = self.list.currentItem()
        if item is None or not item.isSelected():
            return None
        return item.data(Qt.UserRole)

    def _refresh_all(self, select: str | None = None):
        keep = select or self._selected_iso()
        self._reload()
        self._populate_list(keep)
        self._update_ui()
        self._update_month_label()

    def _update_ui(self):
        iso = self._selected_iso()
        fsf, sfs = self.assign.get(iso, ("", "")) if iso else ("", "")
        recorded = bool(fsf or sfs)
        self._btn_add.setEnabled(iso is not None and not recorded)
        self._btn_modify.setEnabled(iso is not None and recorded)
        self._btn_remove.setEnabled(iso is not None and recorded)
        self._btn_cancel.setEnabled(iso is not None)

    def _on_cancel(self):
        self.list.clearSelection()
        self._update_ui()

    def _on_select(self):
        self._update_ui()

    def _on_edit_or_add(self):
        iso = self._selected_iso()
        if not iso:
            return
        if any(self.assign.get(iso, ("", ""))):
            self._on_modify()
        else:
            self._on_add()

    def _assignment_dialog(self, iso_ds: str, fsf: str, sfs: str, save_cb):
        self.nm.refresh_cache()
        nurses = self.nm.get_nurses()
        dlg = ToolDialog(self.parent, "Weekend Assignment")
        dlg.resize(460, 300)
        form = QFormLayout()
        form.setSpacing(10)
        friday = date.fromisoformat(iso_ds)
        form.addRow(
            "Weekend:", QLabel(f"{friday:%a %b %d} – {friday + timedelta(days=2):%a %b %d, %Y}")
        )
        cbm = nurse_combo(nurses, fsf)
        cbm.setToolTip("Works Friday and Sunday")
        form.addRow("FSF (Fri + Sun):", cbm)
        cbb = nurse_combo(nurses, sfs)
        cbb.setToolTip("Works Saturday")
        form.addRow("SFS (Sat):", cbb)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)

        def _save():
            new_fsf, new_sfs = cbm.currentData() or "", cbb.currentData() or ""
            if not new_fsf or not new_sfs:
                show_warning(dlg, "Choose both nurses", "Pick an FSF nurse and an SFS nurse.")
                return
            if new_fsf == new_sfs:
                show_warning(dlg, "Choose two nurses", f"{new_fsf} cannot work both FSF and SFS.")
                return
            save_cb(new_fsf, new_sfs)
            dlg.accept()
            self._refresh_all(select=iso_ds)

        btns.accepted.connect(_save)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        dlg.setLayout(form)
        dlg.open()

    def _on_add(self):
        iso = self._selected_iso()
        if not iso:
            return
        self._assignment_dialog(
            iso, "", "", save_cb=lambda fsf, sfs: self.wh.add_assignment(iso, fsf, sfs)
        )

    def _on_modify(self):
        iso = self._selected_iso()
        if not iso:
            return
        fsf, sfs = self.assign.get(iso, ("", ""))
        self._assignment_dialog(
            iso,
            fsf,
            sfs,
            # INSERT OR REPLACE: also repairs rows where one nurse is missing.
            save_cb=lambda new_fsf, new_sfs: self.wh.add_assignment(iso, new_fsf, new_sfs),
        )

    def _on_remove(self):
        iso = self._selected_iso()
        if not iso or not any(self.assign.get(iso, ("", ""))):
            return
        friday = date.fromisoformat(iso)
        confirm(
            self,
            "Remove weekend",
            f"Remove the record for the weekend of {friday:%a %b %d, %Y}?\n\n"
            "Rotation violation counts are recalculated from the remaining weekends.",
            yes_cb=lambda: (self.wh.remove_assignment(iso), self._refresh_all()),
            yes_text="Remove",
            destructive=True,
        )

    def _on_sync(self):
        run_weekend_sync(self, self.parent.backend, after=self._refresh_all)


__all__ = ["WeekendHistoryCalendarScreen"]
