"""Nurse management screen — add/remove/edit nurses and their unavailable dates."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager

from ..config import DB_NAME
from ..dialogs.tool_dialog import ToolDialog
from ..messages import confirm, show_warning
from ..widgets.common import (
    action_button,
    add_shortcut,
    back_button,
    install_empty_state,
    screen_title,
)
from ..widgets.date_pickers import MultiDatePicker


def _nurse_label(name: str, prn: bool, late: bool) -> str:
    tags = [tag for tag, on in (("PRN", prn), ("Late shift", late)) if on]
    return f"{name}   ·   {', '.join(tags)}" if tags else name


class NurseManagementScreen(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.nm = NurseManager(DB_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(screen_title("Manage Nurses"))
        hint = QLabel("Double-click a nurse to change PRN/Late status.")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        # Nurse list
        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 16))
        self.list.setSpacing(4)
        self.list.setStyleSheet("QListWidget { padding:6px; }")
        install_empty_state(self.list, "No nurses yet.\nClick “Add Nurse” to build the roster.")
        layout.addWidget(self.list, 1)

        # Buttons grid
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._btn_add = action_button("Add Nurse", "special", tooltip="Add a nurse (Ctrl+N)")
        self._btn_status = action_button(
            "Edit Status…", tooltip="Set PRN / late-shift status (Enter)"
        )
        self._btn_edit = action_button(
            "Edit Unavailable Dates…", tooltip="Pick the dates this nurse cannot work"
        )
        self._btn_remove = action_button(
            "Remove Nurse", "destructive", tooltip="Take this nurse off the roster (Delete)"
        )
        grid.addWidget(self._btn_add, 0, 0)
        grid.addWidget(self._btn_status, 0, 1)
        grid.addWidget(self._btn_edit, 1, 0)
        grid.addWidget(self._btn_remove, 1, 1)
        layout.addLayout(grid)

        layout.addWidget(back_button(self, parent))

        # connect
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_edit.clicked.connect(self._on_edit_unavail)
        self._btn_status.clicked.connect(self._on_set_status)
        self.list.itemDoubleClicked.connect(lambda _item: self._on_set_status())
        self.list.itemSelectionChanged.connect(self._update_buttons)
        add_shortcut(self.list, Qt.Key_Delete, self._on_remove)
        add_shortcut(self.list, Qt.Key_Return, self._on_set_status)
        add_shortcut(self, QKeySequence.New, self._on_add)

        self.refresh()

    # ─────────────────── list maintenance ────────────────────
    def on_show(self):
        self.nm.refresh_cache()
        self.refresh()

    def _selected_name(self) -> str | None:
        item = self.list.currentItem()
        if item is None or not item.isSelected():
            return None
        return item.data(Qt.UserRole)

    def refresh(self, select: str | None = None):
        keep = select or self._selected_name()
        self.list.clear()
        for name in self.nm.get_nurses():
            prn = self.nm.get_prn_status(name)
            late = self.nm.get_late_shift_status(name)
            item = QListWidgetItem(_nurse_label(name, prn, late))
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
            if name == keep:
                self.list.setCurrentItem(item)
        self._update_buttons()

    def _update_buttons(self):
        has = self._selected_name() is not None
        for btn in (self._btn_remove, self._btn_edit, self._btn_status):
            btn.setEnabled(has)

    # ─────────────────── Add Nurse (non-blocking) ─────────────
    class AddNurseDialog(ToolDialog):
        """Name plus PRN / late-shift flags, validated before saving."""

        def __init__(self, parent, save_cb):
            super().__init__(parent, "Add Nurse")
            self._save_cb = save_cb
            self.resize(420, 300)

            layout = QVBoxLayout()
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(12)

            layout.addWidget(QLabel("Name"))
            self.edit = QLineEdit()
            self.edit.setPlaceholderText("e.g. Jordan Reyes")
            layout.addWidget(self.edit)

            self.prn = QCheckBox("PRN (as-needed; never given weekend rotations)")
            self.late = QCheckBox("Works late shift (never paired with another late-shift nurse)")
            layout.addWidget(self.prn)
            layout.addWidget(self.late)
            layout.addStretch(1)

            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._on_save)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self.setLayout(layout)

        def _on_save(self):
            name = " ".join(self.edit.text().split())
            if not name:
                show_warning(self, "Name required", "Enter the nurse's name.")
                return
            if self._save_cb(name, self.prn.isChecked(), self.late.isChecked()):
                self.accept()

    def _on_add(self):
        dlg = self.AddNurseDialog(self, save_cb=self._save_new_nurse)
        dlg.open()

    def _save_new_nurse(self, name: str, prn: bool, late: bool) -> bool:
        """Add *name*; returns False (keeping the dialog open) on a duplicate."""
        existing = self.nm.find_nurse(name)
        if existing and existing["is_active"]:
            show_warning(
                self, "Already on the roster", f"{existing['name']} is already on the roster."
            )
            return False
        if existing:
            # A previously removed nurse: bring them back with their history,
            # rather than silently creating what looks like a new person.
            stored = existing["name"]
            confirm(
                self,
                "Restore nurse?",
                f"{stored} was removed earlier. Restore them to the roster with the "
                "PRN/late-shift settings you chose? Their history is unchanged.",
                yes_cb=lambda: self._add_and_select(stored, prn, late),
                yes_text="Restore",
            )
            return True
        self._add_and_select(name, prn, late)
        return True

    def _add_and_select(self, name: str, prn: bool, late: bool) -> None:
        self.nm.add_nurse(name, is_prn=prn, is_late_shift=late)
        self.refresh(select=name)

    # ─────────────────── Remove Nurse ─────────────────────────
    def _on_remove(self):
        name = self._selected_name()
        if not name:
            return
        confirm(
            self,
            "Remove nurse",
            f"Remove {name} from the roster?\n\nThey will no longer be scheduled. "
            "Past assignments and weekend history are kept.",
            yes_cb=lambda: (self.nm.remove_nurse(name), self.refresh()),
            yes_text="Remove",
            destructive=True,
        )

    def _on_edit_unavail(self):
        name = self._selected_name()
        if not name:
            return
        dates = {d.strftime("%Y-%m-%d") for d in self.nm.get_unavailable_dates(name)}

        settings = self.parent.settings
        dlg = ToolDialog(self.parent, title=f"Unavailable dates — {name}")
        v = QVBoxLayout()
        hint = QLabel("Click a date to mark or unmark it.")
        hint.setProperty("role", "muted")
        v.addWidget(hint)

        picker = MultiDatePicker(
            dates, accent=settings.get("accent_color"), theme=settings.get("theme")
        )
        picker.set_theme(
            settings.get("theme"),
            settings.get("accent_color"),
            grid=bool(settings.get("calendar_grid")),
        )
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
        name = self._selected_name()
        if not name:
            return
        prn = self.nm.get_prn_status(name)
        late = self.nm.get_late_shift_status(name)

        dlg = ToolDialog(self.parent, title=f"Status — {name}")
        dlg.resize(420, 260)
        form = QFormLayout()
        cb1 = QCheckBox("PRN (as-needed; never given weekend rotations)")
        cb1.setChecked(prn)
        cb2 = QCheckBox("Works late shift (never paired with another late-shift nurse)")
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
