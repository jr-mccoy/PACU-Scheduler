"""Rotation-violation dialog for selecting which nurses may violate rotation."""

from __future__ import annotations

from PySide6.QtCore import Qt
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
        super().__init__(parent, title="Rotation Violation Settings")

        summary = backend.weekend_history.get_violation_summary()
        summary = summary.sort_values(
            by=["total_viol", "consec_viol", "clean_run_weeks", "days_since_last"],
            ascending=[True, True, False, False],
        ).reset_index(drop=True)
        self.nurses = summary["nurse"].tolist()
        self.stats = summary

        self._accent = accent or getattr(parent, "settings", {}).get("accent_color", "#5C8DBC")
        self._row_chk = []

        root = QVBoxLayout(self._body)
        root.setContentsMargins(18, 18, 18, 10)
        root.setSpacing(6)

        intro = QLabel(
            "Enable rotation violations, then tick the nurses that are permitted "
            "to violate.  Leave all boxes unchecked if NO nurse may violate.",
            wordWrap=True,
            font=QFont("Roboto", 14),
        )
        root.addWidget(intro)

        self.allow_chk = QCheckBox(
            "Allow rotation violations for this run?",
            font=QFont("Roboto", 15, QFont.Bold),
        )
        root.addWidget(self.allow_chk)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setFixedWidth(110)
        self.select_all_btn.setVisible(len(self.nurses) > 8)
        root.addWidget(self.select_all_btn, alignment=Qt.AlignLeft)

        self.list = QListWidget()
        self.list.setFont(QFont("Roboto", 14))
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

        chk_style = f"""
        QCheckBox::indicator         {{ width:28px; height:28px; }}
        QCheckBox::indicator:unchecked {{
            border:2px solid #000;
            background:#fdfdfd;
            border-radius:4px;
        }}
        QCheckBox::indicator:checked  {{
            border:2px solid #000;
            background:{self._accent};
            border-radius:4px;
        }}
        """

        for _, row in summary.iterrows():
            n = row["nurse"]
            label = (
                f"{n}  (Viol: {row['total_viol']}, Streak: {row['consec_viol']}, "
                f"Clean: {row['clean_run_weeks']}, Days: {row['days_since_last']})"
            )
            roww = QWidget()
            hl = QHBoxLayout(roww)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)
            cb = QCheckBox()
            cb.setFixedSize(28, 28)
            cb.setStyleSheet(chk_style)
            lbl = QLabel(label)
            lbl.setFont(QFont("Roboto", 14))
            hl.addWidget(cb)
            hl.addWidget(lbl)
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

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        h = QHBoxLayout()
        h.addStretch()
        h.addWidget(btn_box)
        h.addStretch()
        root.addLayout(h)

        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        self.setMinimumHeight(500)
        self.setMaximumHeight(1200)

        self._wire_signals()
        self._apply_accent()

    def _wire_signals(self):
        self.allow_chk.toggled.connect(self.list.setEnabled)
        self.list.setEnabled(False)
        self.select_all_btn.clicked.connect(lambda: [cb.setChecked(True) for cb in self._row_chk])

    def _toggle_row(self, item):
        idx = self.list.row(item)
        if 0 <= idx < len(self._row_chk):
            cb = self._row_chk[idx]
            cb.setChecked(not cb.isChecked())
        self.list.clearSelection()

    def _apply_accent(self):
        self.select_all_btn.setStyleSheet(
            "QPushButton {"
            f"background:{self._accent};"
            "color:#fff;"
            "border-radius:8px;"
            "padding:6px 12px;"
            "}"
            "QPushButton:pressed {"
            "opacity:0.8;"
            "}"
        )

    def refresh_accent(self, accent=None):
        if accent:
            self._accent = accent
        self._apply_accent()

    def set_theme(self, theme, accent):
        self._accent = accent
        self._apply_accent()

    def get_values(self):
        if not self.allow_chk.isChecked():
            return (False, [])
        allowed = [self.nurses[i] for i, cb in enumerate(self._row_chk) if cb.isChecked()]
        return (True, allowed)


__all__ = ["RotationViolationDialog"]
