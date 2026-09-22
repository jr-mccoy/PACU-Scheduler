"""Assignment history screen."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from scheduler import AssignmentHistory, NurseManager

from ..config import DB_NAME
from ..dialogs.assignment_dialog import display_date, open_assignment_dialog
from ..messages import confirm, show_info, show_warning
from ..widgets.common import action_button
from .assignment_table import AssignmentTableScreen

SYNC_LABEL = "Copy Weekend History → Assignment History"
SYNC_TOOLTIP = (
    "Fill in the Friday–Sunday rows of assignment history from weekend history, "
    "replacing any day that disagrees"
)


def run_weekend_sync(screen, backend, after=None) -> None:
    """Confirm, copy weekend history into assignment history, and report counts."""

    def _do():
        try:
            report = backend.sync_assignment_history_with_weekend() or {}
        except Exception as e:
            show_warning(screen, "Copy failed", f"Weekend history could not be copied: {e}")
        else:
            added = report.get("added", 0)
            replaced = report.get("conflicts_overwritten", 0)
            if added or replaced:
                show_info(
                    screen,
                    "Weekend history copied",
                    f"Added {added} day{'s' if added != 1 else ''} and replaced "
                    f"{replaced} that disagreed with weekend history.",
                )
            else:
                show_info(screen, "Already in sync", "Assignment history already matches.")
        if after:
            after()

    confirm(
        screen,
        "Copy weekend history",
        "Copy every Friday–Sunday rotation from weekend history into assignment "
        "history?\n\nDays where assignment history disagrees will be overwritten.",
        yes_cb=_do,
        yes_text="Copy",
    )


class AssignmentHistoryScreen(AssignmentTableScreen):
    TITLE = "Assignment History"
    SUBTITLE = "Past Main/Backup shifts. Recent history balances workload in new schedules."
    COLUMNS = ["Date", "Main", "Backup"]
    EMPTY_TEXT = (
        "No assignment history yet.\nApplying a generated schedule records it here, "
        "or copy it from weekend history below."
    )
    SCROLL_TO_END = True

    def __init__(self, parent):
        self.ah = AssignmentHistory(DB_NAME)
        self.nm = NurseManager(DB_NAME)
        super().__init__(parent)

    def add_extra_buttons(self, layout: QVBoxLayout) -> None:
        self.btn_sync = action_button(SYNC_LABEL, tooltip=SYNC_TOOLTIP)
        self.btn_sync.clicked.connect(self._on_sync)
        layout.addWidget(self.btn_sync)

    def load_rows(self):
        return self.ah.get_history()

    def _nurses(self) -> list[str]:
        self.nm.refresh_cache()
        return self.nm.get_nurses()

    def _save(self, ds, main, backup, _note):
        self.ah.update_history(ds, main or "", backup or "")
        self.refresh(select=ds)

    def _on_add(self):
        open_assignment_dialog(
            self.parent,
            title="Add History Day",
            nurses=self._nurses(),
            existing_dates=self.existing_dates(),
            on_save=self._save,
        )

    def _on_modify(self):
        iso = self.selected_iso()
        if not iso:
            return
        rec = self.ah.get_record(iso)
        if not rec:
            self.refresh()
            return
        m, b = rec
        open_assignment_dialog(
            self.parent,
            title="Edit History Day",
            nurses=self._nurses(),
            on_save=self._save,
            iso_date=iso,
            main=m,
            backup=b,
        )

    def _on_remove(self):
        iso = self.selected_iso()
        if not iso:
            return
        confirm(
            self,
            "Remove history day",
            f"Remove the assignment history for {display_date(iso)}?",
            yes_cb=lambda: (self.ah.delete_record(iso), self.refresh()),
            yes_text="Remove",
            destructive=True,
        )

    def _on_sync(self):
        run_weekend_sync(self, self.parent.backend, after=self.refresh)


__all__ = ["AssignmentHistoryScreen", "run_weekend_sync", "SYNC_LABEL"]
