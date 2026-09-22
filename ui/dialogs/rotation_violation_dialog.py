"""Rotation-violation dialog for selecting which nurses may violate rotation."""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .tool_dialog import ToolDialog


class RotationViolationDialog(ToolDialog):
    """Enable/disable rotation-rule violations and pick which nurses may violate."""

    def __init__(self, parent, backend, accent=None, theme=None):
        super().__init__(parent, title="Rotation Rules for This Run")

        summary = backend.weekend_history.get_violation_summary()
        # Only nurses who can actually be given a weekend: active and not PRN.
        try:
            eligible = set(backend.nurse_manager.get_non_prn_nurses())
            summary = summary[summary["nurse"].isin(eligible)]
        except Exception:
            pass
        summary = summary.sort_values(
            by=["total_viol", "consec_viol", "clean_run_weeks", "days_since_last"],
            ascending=[True, True, False, False],
        ).reset_index(drop=True)
        self.nurses = summary["nurse"].tolist()
        self.stats = summary

        parent_settings = getattr(parent, "settings", None)
        self._accent = accent or (
            parent_settings.get("accent_color") if parent_settings is not None else "#5C8DBC"
        )
        self._row_chk = []

        root = QVBoxLayout(self._body)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        intro = QLabel(
            "Each nurse normally alternates between FSF and SFS weekends. If that "
            "leaves no workable schedule, you can let chosen nurses repeat their last "
            "pattern in this run. Fewest past violations are listed first.",
            wordWrap=True,
        )
        root.addWidget(intro)

        self.allow_chk = QCheckBox("Allow repeats for the ticked nurses")
        font = self.allow_chk.font()
        font.setBold(True)
        self.allow_chk.setFont(font)
        root.addWidget(self.allow_chk)

        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_none_btn = QPushButton("Select None")
        for b in (self.select_all_btn, self.select_none_btn):
            b.setAutoDefault(False)
            sel_row.addWidget(b)
        sel_row.addStretch()
        root.addLayout(sel_row)

        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 13))
        self.list.setAlternatingRowColors(False)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setSpacing(0)
        self.list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.list.setStyleSheet("QListWidget::item:selected { background: transparent; }")

        try:
            from PySide6.QtWidgets import QScroller

            QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)
        except Exception:
            pass

        for _, row in summary.iterrows():
            n = row["nurse"]
            viol = int(row["total_viol"])
            clean = int(row["clean_run_weeks"])
            detail = (
                "never violated"
                if clean >= 999
                else (
                    f"{viol} violation{'s' if viol != 1 else ''}, streak {int(row['consec_viol'])}, "
                    f"{clean} clean weekend{'s' if clean != 1 else ''}"
                )
            )
            roww = QWidget()
            hl = QHBoxLayout(roww)
            hl.setContentsMargins(4, 2, 4, 2)
            hl.setSpacing(8)
            cb = QCheckBox()
            cb.setAccessibleName(f"Allow {n} to repeat")
            name_lbl = QLabel(f"<b>{n}</b>")
            detail_lbl = QLabel(detail)
            detail_lbl.setProperty("role", "muted")
            hl.addWidget(cb)
            hl.addWidget(name_lbl)
            hl.addWidget(detail_lbl)
            hl.addStretch()
            itm = QListWidgetItem()
            itm.setSizeHint(roww.sizeHint())
            self.list.addItem(itm)
            self.list.setItemWidget(itm, roww)
            self._row_chk.append(cb)

        hard_cap = 700
        row_h = self.list.sizeHintForRow(0) or 36
        if row_h * len(self.nurses) > hard_cap:
            self.list.setMaximumHeight(hard_cap)
        root.addWidget(self.list, stretch=1)

        self.list.itemClicked.connect(self._toggle_row)

        btn_box = QDialogButtonBox()
        start_btn = btn_box.addButton("Start Generating", QDialogButtonBox.AcceptRole)
        start_btn.setProperty("role", "special")
        start_btn.setDefault(True)
        btn_box.addButton(QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

        self.setMinimumWidth(480)
        self.setMaximumWidth(720)
        self.resize(600, 640)
        self.setMinimumHeight(420)
        self.setMaximumHeight(1200)

        self._wire_signals()
        self._apply_accent()

    def _wire_signals(self):
        for w in (self.list, self.select_all_btn, self.select_none_btn):
            self.allow_chk.toggled.connect(w.setEnabled)
            w.setEnabled(False)
        self.select_all_btn.clicked.connect(lambda: self._set_all(True))
        self.select_none_btn.clicked.connect(lambda: self._set_all(False))

    def _set_all(self, checked: bool) -> None:
        for cb in self._row_chk:
            cb.setChecked(checked)

    def _toggle_row(self, item):
        idx = self.list.row(item)
        if 0 <= idx < len(self._row_chk):
            cb = self._row_chk[idx]
            cb.setChecked(not cb.isChecked())
        self.list.clearSelection()

    def _apply_accent(self):
        # Checkbox colours come from the card style (theme-aware); nothing extra.
        pass

    def refresh_accent(self, accent=None):
        if accent:
            self._accent = accent
        super().refresh_accent(accent)

    def set_theme(self, theme, accent):
        self._accent = accent
        self._apply_accent()

    def get_values(self):
        if not self.allow_chk.isChecked():
            return (False, [])
        allowed = [self.nurses[i] for i, cb in enumerate(self._row_chk) if cb.isChecked()]
        return (True, allowed)


__all__ = ["RotationViolationDialog"]
