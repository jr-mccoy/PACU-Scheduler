"""View All Unavailable screen — searchable list of nurse unavailable dates."""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScroller,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from scheduler import NurseManager

from ..config import DB_NAME
from ..dialogs.tool_dialog import ToolDialog
from ..messages import show_error, show_warning
from ..theme import shade_color
from ..widgets.common import (
    action_button,
    add_shortcut,
    back_button,
    install_empty_state,
    screen_title,
)
from ..widgets.date_pickers import MultiDatePicker


class ViewAllUnavailableScreen(QWidget):
    _SPAN_RE = re.compile(r"color:\s*#[0-9A-Fa-f]{6}")

    # ───────────────────────────  ctor  ────────────────────────────
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        try:
            self.nm = NurseManager(DB_NAME)
        except Exception as e:
            show_error(self, "Database Error", str(e))
            self.nm = None

        # ui skeleton ------------------------------------------------------
        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(12)

        main.addWidget(screen_title("Unavailable Dates"))

        self.search = QLineEdit(placeholderText="Search by nurse name…", font=QFont("Roboto", 15))
        self.search.setClearButtonEnabled(True)
        main.addWidget(self.search)

        self.show_past = QCheckBox("Include past dates")
        self.show_past.setToolTip("Past time off does not affect new schedules")
        main.addWidget(self.show_past)

        self.list = QListWidget(
            verticalScrollMode=QAbstractItemView.ScrollPerPixel,
            horizontalScrollBarPolicy=Qt.ScrollBarAlwaysOff,
            spacing=6,
            frameShape=QFrame.NoFrame,
        )
        self._empty = install_empty_state(self.list, "")
        main.addWidget(self.list, 1)

        QScroller.grabGesture(self.list.viewport(), QScroller.TouchGesture)

        self.edit_btn = action_button(
            "Edit Dates…", "special", tooltip="Change the selected nurse's time off (Enter)"
        )
        main.addWidget(self.edit_btn)

        main.addWidget(back_button(self, parent))

        # signals ----------------------------------------------------------
        self.search.textChanged.connect(self._reload_list)
        self.show_past.toggled.connect(self._reload_list)
        self.list.currentItemChanged.connect(self._on_select_change)
        self.list.itemDoubleClicked.connect(lambda _i: self._on_edit())
        self.edit_btn.clicked.connect(self._on_edit)
        add_shortcut(self.list, Qt.Key_Return, self._on_edit)

        # data -> first populate ------------------------------------------
        self._all_data: list[tuple[str, bool, list[str]]] = []
        self._load_all()
        self._apply_theme()

    def on_show(self):
        if self.nm:
            self.nm.refresh_cache()
        self._load_all()

    # ─────────────── theme palette helper ────────────────
    @property
    def _colours(self):
        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        if theme == "dark":
            bg, edge = "#2D3238", shade_color("#2D3238", 1.15)
            act_bg, act_edge = accent, shade_color(accent, 0.80)
            month_idle, month_sel = accent, "#FFFFFF"
        elif theme == "light":
            bg, edge = "#FFFFFF", "#E1DDD6"
            act_bg, act_edge = accent, shade_color(accent, 0.80)
            month_idle, month_sel = accent, "#FFFFFF"
        else:  # pink
            bg, edge = "#FFE6E6", "#F4C2C2"
            act_bg, act_edge = "#FFBFD2", "#E88AA5"
            month_idle = month_sel = "#AA5577"

        return bg, edge, act_bg, act_edge, month_idle, month_sel

    # ─────────────── card factory ────────────────────────
    def _build_card(self, name: str, iso_dates: list[str], prn: bool = False) -> QWidget:
        bg, edge, *_, month_idle, _ = self._colours

        tag = " &nbsp;<i>(PRN)</i>" if prn else ""
        count = f" &nbsp;·&nbsp; {len(iso_dates)} day{'s' if len(iso_dates) != 1 else ''}"
        html = [f"<b>{name}</b>{tag}{count if iso_dates else ''}"]
        if iso_dates:
            groups = defaultdict(list)
            for iso in iso_dates:
                y, m, d = iso.split("-")
                groups[(int(y), int(m))].append(int(d))
            for (yr, mo), days in sorted(groups.items()):
                span = (
                    f'<span style="font-weight:600;color:{month_idle};">'
                    f"{calendar.month_abbr[mo]} {yr}:</span>"
                )
                for i in range(0, len(days := sorted(days)), 8):
                    chunk = ", ".join(map(str, days[i : i + 8]))
                    html.append(f"{span if i == 0 else '&nbsp;&nbsp;'} {chunk}")
        else:
            html.append("No time off" if self.show_past.isChecked() else "No upcoming time off")

        card = QWidget()
        card.setStyleSheet(self._card_css(bg, edge))
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lbl = QLabel(
            "<br>".join(html), wordWrap=True, textFormat=Qt.RichText, font=QFont("Roboto", 15)
        )
        lay.addWidget(lbl)
        return card

    @staticmethod
    def _card_css(bg, edge):
        return f"background:{bg};border:1px solid {edge};border-radius:8px;"

    # helper to change month colour ---------------------------------------
    @classmethod
    def _tint_month(cls, card: QWidget, colour: str):
        lbl = card.findChild(QLabel)
        if lbl:
            lbl.setText(cls._SPAN_RE.sub(f"color:{colour}", lbl.text()))

    # ─────────────── list population ────────────────────
    def _load_all(self):
        if not self.nm:
            return
        self._all_data = []
        for name in sorted(self.nm.nurses, key=str.casefold):
            info = self.nm.nurses[name]
            iso = sorted(d.strftime("%Y-%m-%d") for d in info["unavailable_dates"])
            self._all_data.append((name, self.nm.is_prn_nurse(name), iso))
        self._reload_list()

    def _selected_name(self) -> str | None:
        itm = self.list.currentItem()
        return itm.data(Qt.UserRole) if itm is not None else None

    def _reload_list(self, *_):
        keep = self._selected_name()
        term = self.search.text().lower().strip()
        today = date.today().isoformat()
        past = self.show_past.isChecked()
        self.list.blockSignals(True)
        self.list.clear()

        restore = None
        for name, prn, iso_list in self._all_data:
            if term and term not in name.lower():
                continue
            shown = iso_list if past else [d for d in iso_list if d >= today]
            card = self._build_card(name, shown, prn)

            itm = QListWidgetItem()
            itm.setData(Qt.UserRole, name)
            self.list.addItem(itm)
            self.list.setItemWidget(itm, card)
            if name == keep:
                restore = itm

        if not self._all_data:
            self._empty.text = "No nurses yet. Add them under Manage Nurses."
        else:
            self._empty.text = f"No nurse matches “{self.search.text().strip()}”."
        self.list.blockSignals(False)
        self.edit_btn.setEnabled(False)
        if restore is not None:
            self.list.setCurrentItem(restore)

        QTimer.singleShot(0, self._fix_item_sizes)

    # ─────────────── dynamic row-size fixer ─────────────
    def _fix_item_sizes(self):
        vw = self.list.viewport().width()  # current usable width
        for i in range(self.list.count()):
            itm = self.list.item(i)
            card = self.list.itemWidget(itm)
            if not card:
                continue
            card.setFixedWidth(vw)  # enforce exact width
            card.layout().activate()  # recalc wrapping
            # sizeHint ignores word wrap; ask for the height at this width.
            height = (
                card.heightForWidth(vw) if card.hasHeightForWidth() else card.sizeHint().height()
            )
            card.setFixedHeight(height)
            itm.setSizeHint(QSize(vw, height))

    # also call it on every list resize ------------------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fix_item_sizes)

    # ─────────────── selection highlight ────────────────
    def _on_select_change(self, cur, prev):
        bg, edge, act_bg, act_ed, month_idle, month_sel = self._colours
        if prev:
            pc = self.list.itemWidget(prev)
            pc.setStyleSheet(self._card_css(bg, edge))
            self._tint_month(pc, month_idle)
        if cur:
            cc = self.list.itemWidget(cur)
            cc.setStyleSheet(self._card_css(act_bg, act_ed))
            self._tint_month(cc, month_sel)
        self.edit_btn.setEnabled(cur is not None and self.nm is not None)

    # ─────────────── theme refresh from App ─────────────
    def apply_theme_update(self):
        self._apply_theme()
        self._reload_list()

    def _apply_theme(self):
        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        if theme == "dark":
            bg, txt, sel_bg, sel_txt = "#252A32", "#E8EAF0", accent, "#FFFFFF"
        elif theme == "light":
            bg, txt, sel_bg, sel_txt = "#FFFFFF", "#2C2A27", accent, "#FFFFFF"
        else:
            bg, txt, sel_bg, sel_txt = "#F7D7DF", "#4A4A4A", "#E75480", "#FFFFFF"

        self.list.setStyleSheet(f"""
            QListWidget {{
                background:{bg};
                color:{txt};
                border:none;
            }}
            QListWidget::item {{
                padding:6px 8px; border-radius:8px;
            }}
            QListWidget::item:selected {{
                background:{sel_bg};
                color:{sel_txt};
            }}""")

    # ─────────────── edit handler ──────────
    def _on_edit(self):
        if not self.nm:
            return
        itm = self.list.currentItem()
        if not itm:
            return
        name = itm.data(Qt.UserRole)
        try:
            existing = {d.strftime("%Y-%m-%d") for d in self.nm.get_unavailable_dates(name)}
        except Exception as e:
            show_warning(self, "Database Error", str(e))
            return

        accent = self.parent.settings.get("accent_color")
        theme = self.parent.settings.get("theme")

        dlg = ToolDialog(self.parent, f"Unavailable dates — {name}")
        v = QVBoxLayout()
        hint = QLabel("Click a date to mark or unmark it.")
        hint.setProperty("role", "muted")
        v.addWidget(hint)
        picker = MultiDatePicker(existing, accent=accent, theme=theme)
        picker.set_theme(theme, accent, grid=bool(self.parent.settings.get("calendar_grid")))
        v.addWidget(picker)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)

        def _save():
            try:
                self.nm.update_unavailable_dates(name, picker.selected)
                dlg.accept()
                self._load_all()
            except Exception as e:
                show_error(dlg, "Database Error", str(e))

        btns.accepted.connect(_save)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        dlg.setLayout(v)
        dlg.open()


__all__ = ["ViewAllUnavailableScreen"]
