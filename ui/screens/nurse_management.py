"""Nurse management screen — add/remove/edit nurses and their unavailable dates."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager

from ..config import DB_NAME
from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_warning
from ..style import UiStyle
from ..widgets.date_pickers import MultiDatePicker


class NurseManagementScreen(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.nm = NurseManager(DB_NAME)

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
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._btn_add = QPushButton("Add Nurse")
        self._btn_remove = QPushButton("Remove Nurse")
        self._btn_remove.setProperty("role", "destructive")
        self._btn_edit = QPushButton("Edit Unavail")
        self._btn_edit.setProperty("role", "special")
        self._btn_status = QPushButton("Set Status")
        for btn in (self._btn_add, self._btn_remove, self._btn_edit, self._btn_status):
            btn.setMinimumHeight(48)
            grid.addWidget(
                btn,
                0 if btn in (self._btn_add, self._btn_remove) else 1,
                0 if btn in (self._btn_add, self._btn_edit) else 1,
            )
        layout.addLayout(grid)

        back = QPushButton("Back")
        back.setMinimumHeight(48)
        back.setProperty("role", "special")
        back.clicked.connect(lambda: parent.switch_frame("main"))
        layout.addWidget(back)

        # connect
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_edit.clicked.connect(self._on_edit_unavail)
        self._btn_status.clicked.connect(self._on_set_status)

        self.refresh()

    # ─────────────────── list maintenance ────────────────────
    def refresh(self):
        self.list.clear()
        for name in self.nm.get_nurses():
            prn = self.nm.get_prn_status(name)
            late = self.nm.get_late_shift_status(name)
            label = name
            if prn:
                label += " [PRN]"
            if late:
                label += " [Late]"
            self.list.addItem(label)

    # ─────────────────── Add Nurse (non-blocking) ─────────────
    class AddNurseDialog(ToolDialog):
        """Fixed version of the Add Nurse dialog"""

        def __init__(self, parent, save_cb):
            super().__init__(parent, "Add Nurse")
            self._save_cb = save_cb

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

            self.edit.setFocus()

        def _on_save(self):
            name = self.edit.text().strip()
            if name:
                self._save_cb(name)
                self.accept()
            else:
                show_warning(self, "Error", "Please enter a nurse name.")

    def _on_add(self):
        dlg = self.AddNurseDialog(self, save_cb=lambda n: (self.nm.add_nurse(n), self.refresh()))
        dlg.open()

    # ─────────────────── Remove Nurse ─────────────────────────
    def _on_remove(self):
        item = self.list.currentItem()
        if not item:
            return
        name = item.text().split(" [")[0]
        confirm(
            self,
            "Confirm",
            f"Remove {name}?",
            yes_cb=lambda: (self.nm.remove_nurse(name), self.refresh()),
        )

    def _on_edit_unavail(self):
        item = self.list.currentItem()
        if not item:
            return
        name = item.text().split(" [")[0]
        dates = {d.strftime("%Y-%m-%d") for d in self.nm.get_unavailable_dates(name)}

        accent = self.parent.settings.get("accent_color")
        theme = self.parent.settings.get("theme")

        dlg = ToolDialog(self.parent, title=f"Unavailable: {name}")
        v = QVBoxLayout()

        picker = MultiDatePicker(dates, accent=accent, theme=theme)
        v.addWidget(picker)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(
            lambda: (
                self.nm.update_unavailable_dates(name, picker.selected),
                dlg.accept(),
                self.refresh(),
            )
        )
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
        prn = self.nm.get_prn_status(name)
        late = self.nm.get_late_shift_status(name)

        dlg = ToolDialog(self.parent, title=f"Status: {name}")
        form = QFormLayout()
        cb1 = QCheckBox("PRN")
        cb1.setChecked(prn)
        cb2 = QCheckBox("Late Shift")
        cb2.setChecked(late)
        form.addRow(cb1)
        form.addRow(cb2)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(
            lambda: (
                self.nm.set_prn_status(name, cb1.isChecked()),
                self.nm.set_late_shift_status(name, cb2.isChecked()),
                dlg.accept(),
                self.refresh(),
            )
        )
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        dlg.setLayout(form)
        dlg.open()


__all__ = ["NurseManagementScreen"]
