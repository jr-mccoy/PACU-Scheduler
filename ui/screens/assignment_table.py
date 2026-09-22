"""Shared layout and behaviour for the date / Main / Backup table screens.

Pre-scheduled Assignments and Assignment History are the same screen over
different tables: a date-sorted table, Add / Edit / Remove, keyboard
shortcuts, an empty state, and selection that survives a refresh.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScroller,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..widgets.common import (
    action_button,
    add_shortcut,
    back_button,
    install_empty_state,
    screen_title,
)

DATE_FORMAT = "%a %m/%d/%y"


class AssignmentTableScreen(QWidget):
    """Base screen; subclasses provide the data and the add/edit/remove hooks."""

    TITLE = ""
    SUBTITLE = ""
    COLUMNS: list[str] = ["Date", "Main", "Backup"]
    EMPTY_TEXT = ""
    SCROLL_TO_END = False

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self._layout = layout

        layout.addWidget(screen_title(self.TITLE))
        if self.SUBTITLE:
            sub = QLabel(self.SUBTITLE)
            sub.setProperty("role", "muted")
            sub.setAlignment(Qt.AlignCenter)
            sub.setWordWrap(True)
            layout.addWidget(sub)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in range(1, len(self.COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(QFont("Roboto", 14))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        QScroller.grabGesture(self.table.viewport(), QScroller.TouchGesture)
        install_empty_state(self.table, self.EMPTY_TEXT)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self._btn_add = action_button("Add…", "special", tooltip="Add a day (Ctrl+N)")
        self._btn_mod = action_button("Edit…", tooltip="Edit the selected day (Enter)")
        self._btn_del = action_button(
            "Remove", "destructive", tooltip="Remove the selected day (Delete)"
        )
        for b in (self._btn_add, self._btn_mod, self._btn_del):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        self.add_extra_buttons(layout)
        layout.addWidget(back_button(self, parent))

        self._btn_add.clicked.connect(self._on_add)
        self._btn_mod.clicked.connect(self._on_modify)
        self._btn_del.clicked.connect(self._on_remove)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self._on_modify())
        add_shortcut(self.table, Qt.Key_Delete, self._on_remove)
        add_shortcut(self.table, Qt.Key_Return, self._on_modify)
        add_shortcut(self.table, Qt.Key_Enter, self._on_modify)
        add_shortcut(self, QKeySequence.New, self._on_add)

        self.refresh()

    # ─────────────────────────── subclass hooks ───────────────────────────
    def add_extra_buttons(self, layout: QVBoxLayout) -> None:
        """Add screen-specific buttons above Back."""

    def load_rows(self) -> list[tuple]:
        """Return ``(iso_date, main, backup, *extra)`` rows."""
        raise NotImplementedError

    def _on_add(self):
        raise NotImplementedError

    def _on_modify(self):
        raise NotImplementedError

    def _on_remove(self):
        raise NotImplementedError

    # ─────────────────────────── shared behaviour ───────────────────────────
    def on_show(self):
        self.refresh()

    def selected_iso(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item else None

    def existing_dates(self) -> set[str]:
        return {
            self.table.item(r, 0).data(Qt.UserRole)
            for r in range(self.table.rowCount())
            if self.table.item(r, 0) is not None
        }

    def refresh(self, select: str | None = None):
        keep = select or self.selected_iso()
        rows = []
        for iso, *rest in self.load_rows():
            try:
                day = date.fromisoformat(str(iso)[:10])
            except ValueError:
                continue
            rows.append((day, [v or "" for v in rest]))
        rows.sort(key=lambda r: r[0])

        self.table.clearSelection()
        self.table.setRowCount(len(rows))
        select_row = -1
        for i, (day, values) in enumerate(rows):
            iso = day.isoformat()
            date_item = QTableWidgetItem(day.strftime(DATE_FORMAT))
            date_item.setData(Qt.UserRole, iso)
            date_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, date_item)
            for j, txt in enumerate(values[: len(self.COLUMNS) - 1], start=1):
                self.table.setItem(i, j, QTableWidgetItem(txt))
            if iso == keep:
                select_row = i

        if select_row >= 0:
            self.table.selectRow(select_row)
            self.table.scrollToItem(self.table.item(select_row, 0))
        elif self.SCROLL_TO_END and rows:
            self.table.scrollToBottom()
        self._update_buttons()

    def _update_buttons(self):
        has = self.selected_iso() is not None
        self._btn_mod.setEnabled(has)
        self._btn_del.setEnabled(has)


__all__ = ["AssignmentTableScreen", "DATE_FORMAT"]
