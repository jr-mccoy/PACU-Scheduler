"""Pre-scheduled assignments screen."""

from __future__ import annotations

from scheduler import NurseManager, PreScheduler

from ..config import DB_NAME
from ..dialogs.assignment_dialog import display_date, open_assignment_dialog
from ..messages import confirm
from .assignment_table import AssignmentTableScreen


class PreScheduledScreen(AssignmentTableScreen):
    TITLE = "Pre-scheduled Assignments"
    SUBTITLE = "Days pinned in advance. Generated schedules always keep these nurses."
    COLUMNS = ["Date", "Main", "Backup", "Note"]
    EMPTY_TEXT = "Nothing pinned yet.\nClick “Add…” to fix a nurse to a date before generating."

    def __init__(self, parent):
        self.ps = PreScheduler(DB_NAME)
        super().__init__(parent)

    def load_rows(self):
        return self.ps.get_assignments()

    def _nurses(self) -> list[str]:
        return NurseManager(DB_NAME).get_nurses()

    def _save(self, ds, main, backup, note):
        self.ps.add_assignment(ds, main or None, backup or None, note)
        self.refresh(select=ds)

    def _on_add(self):
        open_assignment_dialog(
            self.parent,
            title="Add Pre-scheduled Day",
            nurses=self._nurses(),
            existing_dates=self.existing_dates(),
            on_save=self._save,
            with_note=True,
        )

    def _on_modify(self):
        iso = self.selected_iso()
        if not iso:
            return
        rec = next((r for r in self.ps.get_assignments() if r[0] == iso), None)
        if not rec:
            self.refresh()
            return
        _, m, b, n = rec
        open_assignment_dialog(
            self.parent,
            title="Edit Pre-scheduled Day",
            nurses=self._nurses(),
            on_save=self._save,
            iso_date=iso,
            main=m,
            backup=b,
            note=n,
            with_note=True,
        )

    def _on_remove(self):
        iso = self.selected_iso()
        if not iso:
            return
        confirm(
            self,
            "Remove pinned day",
            f"Remove the pre-scheduled assignment for {display_date(iso)}?",
            yes_cb=lambda: (self.ps.remove_assignment(iso), self.refresh()),
            yes_text="Remove",
            destructive=True,
        )


__all__ = ["PreScheduledScreen"]
