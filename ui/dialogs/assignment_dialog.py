"""Add/edit dialog for a single day's Main/Backup assignment.

Shared by the Pre-scheduled Assignments and Assignment History screens so
both validate the same way: at least one nurse, Main and Backup different,
and an explicit confirmation before overwriting a date that already has a
record.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
)

from ..messages import confirm, show_warning
from ..widgets.date_pickers import SingleDatePicker
from .tool_dialog import ToolDialog

NONE_LABEL = "— none —"


def validate_assignment(main: str, backup: str) -> str | None:
    """Return a user-facing problem with a Main/Backup pair, or None if valid."""
    if not main and not backup:
        return "Choose a Main nurse, a Backup nurse, or both."
    if main and backup and main == backup:
        return f"{main} cannot be both Main and Backup on the same day."
    return None


def display_date(iso: str) -> str:
    """``2026-09-21`` → ``Mon Sep 21, 2026``."""
    try:
        return date.fromisoformat(iso).strftime("%a %b %d, %Y")
    except ValueError:
        return iso


def nurse_combo(nurses: list[str], current: str | None) -> QComboBox:
    cb = QComboBox()
    cb.addItem(NONE_LABEL, "")
    for name in nurses:
        cb.addItem(name, name)
    if current and current not in nurses:
        # Keep a nurse who has since left the roster visible and selectable.
        cb.addItem(f"{current} (not on roster)", current)
    idx = cb.findData(current or "")
    cb.setCurrentIndex(max(idx, 0))
    return cb


def open_assignment_dialog(
    app,
    *,
    title: str,
    nurses: list[str],
    on_save: Callable[[str, str, str, str], None],
    existing_dates: Collection[str] = (),
    iso_date: str | None = None,
    main: str | None = None,
    backup: str | None = None,
    note: str | None = None,
    with_note: bool = False,
) -> ToolDialog:
    """Open the dialog; ``on_save(iso, main, backup, note)`` runs once it validates.

    Pass ``iso_date`` to edit an existing day (the date is then fixed);
    leave it out to add a new one with a date picker.
    """
    settings = app.settings
    dlg = ToolDialog(app, title)
    dlg.resize(460, 620 if iso_date is None else 300)

    form = QFormLayout()
    form.setSpacing(10)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

    picker = None
    if iso_date:
        form.addRow("Date:", QLabel(display_date(iso_date)))
    else:
        picker = SingleDatePicker(
            accent=settings.get("accent_color"),
            initial=QDate.currentDate(),
            theme=settings.get("theme"),
            grid=bool(settings.get("calendar_grid")),
        )
        form.addRow("Date:", picker)

    cbm = nurse_combo(nurses, main)
    form.addRow("Main:", cbm)
    cbb = nurse_combo(nurses, backup)
    form.addRow("Backup:", cbb)
    le = None
    if with_note:
        le = QLineEdit(note or "")
        le.setPlaceholderText("Optional, e.g. reason for the pin")
        form.addRow("Note:", le)

    btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)

    def _commit(ds: str, m: str, b: str, n: str) -> None:
        on_save(ds, m, b, n)
        dlg.accept()

    def _save():
        ds = iso_date or picker.iso()
        m, b = cbm.currentData() or "", cbb.currentData() or ""
        n = le.text().strip() if le is not None else ""
        problem = validate_assignment(m, b)
        if problem:
            show_warning(dlg, "Check the assignment", problem)
            return
        if iso_date is None and ds in existing_dates:
            confirm(
                dlg,
                "Replace existing day?",
                f"{display_date(ds)} already has an assignment. Replace it?",
                yes_cb=lambda: _commit(ds, m, b, n),
                yes_text="Replace",
                destructive=True,
            )
            return
        _commit(ds, m, b, n)

    btns.accepted.connect(_save)
    btns.rejected.connect(dlg.reject)
    form.addRow(btns)

    dlg.setLayout(form)
    dlg.open()
    return dlg


__all__ = ["open_assignment_dialog", "validate_assignment", "display_date", "nurse_combo"]
