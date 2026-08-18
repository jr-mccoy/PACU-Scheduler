"""Phone-friendly compact settings dialog."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..widgets.common import WrappedCheck
from .tool_dialog import ToolDialog

try:  # pragma: no cover - environment-dependent
    from PySide6.QtGui import QGuiApplication as _AppScreen
except Exception:  # pragma: no cover
    from PySide6.QtWidgets import QApplication as _AppScreen


class CompactSettingsDialog(ToolDialog):
    """Phone-friendly settings dialog.

    * Vertical scroll only (no horizontal scroll)
    * Buttons pinned and always visible
    * Form rows wrap on narrow screens
    * Pages do not over-expand vertically
    """

    def __init__(self, settings, parent=None):
        super().__init__(parent, title="Settings")
        self.settings = settings

        body = QVBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        bar = self.tabs.tabBar()
        bar.setExpanding(True)
        bar.setElideMode(Qt.ElideRight)
        body.addWidget(self.tabs)

        def make_form(parent=None):
            form = QFormLayout(parent)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(10)
            form.setContentsMargins(6, 6, 6, 6)
            return form

        ui_tab = QWidget()
        ui_form = make_form(ui_tab)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light", "pink"])
        self.theme_combo.setCurrentText(settings.get("theme"))
        self.theme_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ui_form.addRow("Theme:", self.theme_combo)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 32)
        self.font_spin.setValue(settings.get("font_size"))
        self.font_spin.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.font_spin.setMinimumWidth(0)
        ui_form.addRow("Font size:", self.font_spin)

        self.accent_edit = QLineEdit(settings.get("accent_color"))
        self.accent_edit.setMinimumWidth(0)
        self.accent_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.accent_swatch = QLabel()
        self.accent_swatch.setFixedSize(28, 28)
        self.accent_swatch.setStyleSheet(
            f"""
            background: {settings.get('accent_color')};
            border: 2px solid #888; border-radius: 6px;
        """
        )

        acc_row = QWidget()
        acc_lay = QHBoxLayout(acc_row)
        acc_lay.setContentsMargins(0, 0, 0, 0)
        acc_lay.setSpacing(8)
        acc_lay.addWidget(self.accent_edit, 1)
        acc_lay.addWidget(self.accent_swatch, 0)
        ui_form.addRow("Accent color:", acc_row)

        def update_swatch():
            color = self.accent_edit.text()
            self.accent_swatch.setStyleSheet(
                f"""
                background: {color if color.startswith('#') and len(color) == 7 else '#5C8DBC'};
                border: 2px solid #888; border-radius: 6px;
            """
            )

        self.accent_edit.textChanged.connect(update_swatch)

        self.gif_chk = QCheckBox("Show animated GIF")
        self.gif_chk.setChecked(settings.get("show_gif"))
        ui_form.addRow("", self.gif_chk)

        self.grid_chk = QCheckBox("Show calendar grid")
        self.grid_chk.setChecked(settings.get("calendar_grid"))
        ui_form.addRow("", self.grid_chk)

        self.tabs.addTab(ui_tab, "UI")

        sched_tab = QWidget()
        sched_form = make_form(sched_tab)

        def sb(val, lo, hi):
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setValue(val)
            box.setMinimumHeight(40)
            box.setMinimumWidth(0)
            box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            return box

        self.weekend_gap = sb(settings.get("weekend_gap_days"), 7, 90)
        self.min_between = sb(settings.get("min_days_between_assignments"), 0, 7)
        self.main_factor = sb(settings.get("main_score_factor"), 1, 100)
        self.backup_factor = sb(settings.get("backup_score_factor"), 1, 100)
        self.avail_penalty = sb(settings.get("availability_penalty"), 0, 100)
        self.hist_window = sb(settings.get("history_window_days"), 1, 365)
        self.hist_duration = sb(settings.get("history_duration_months"), 1, 60)

        sched_form.addRow("Weekend gap (days):", self.weekend_gap)
        sched_form.addRow("Min days between:", self.min_between)
        sched_form.addRow("Main score factor:", self.main_factor)
        sched_form.addRow("Backup score factor:", self.backup_factor)
        sched_form.addRow("Availability penalty:", self.avail_penalty)
        sched_form.addRow("History window (days):", self.hist_window)
        sched_form.addRow("History duration (mo):", self.hist_duration)

        self.tabs.addTab(sched_tab, "Schedule")

        debug_tab = QWidget()
        dbg_outer = QVBoxLayout(debug_tab)
        dbg_outer.setContentsMargins(6, 6, 6, 6)
        dbg_outer.setSpacing(8)

        self.measure_chk = QCheckBox("Measure phase times")
        self.measure_chk.setChecked(settings.get("measure_phase_times"))
        self.analyse_chk = QCheckBox("Analyse initial gaps")
        self.analyse_chk.setChecked(settings.get("analyse_initial_weekday_gaps"))
        self.assignment_debug_chk = QCheckBox(
            "Enable structured assignment debug logging"
        )
        self.assignment_debug_chk.setChecked(settings.get("assignment_debug_enabled"))
        dbg_outer.addWidget(self.measure_chk)
        dbg_outer.addWidget(self.analyse_chk)
        dbg_outer.addWidget(self.assignment_debug_chk)

        dbg_form = make_form()
        self.debug_mode_combo = QComboBox()
        self.debug_mode_combo.addItem("Off", "off")
        self.debug_mode_combo.addItem("Pairs only", "pairs")
        self.debug_mode_combo.addItem("Variants only", "variants")
        self.debug_mode_combo.addItem("Pairs + variants", "all")
        current_mode = settings.get("debug_variant_logging")
        idx = self.debug_mode_combo.findData(current_mode)
        if idx < 0:
            idx = 0
        self.debug_mode_combo.setCurrentIndex(idx)
        dbg_form.addRow("Variant debug:", self.debug_mode_combo)
        self.gap_file = QLineEdit(settings.get("gap_report_file"))
        self.gap_file.setMinimumWidth(0)
        self.gap_file.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dbg_form.addRow("Gap report file:", self.gap_file)
        dbg_outer.addLayout(dbg_form)
        dbg_outer.addStretch()

        self.tabs.addTab(debug_tab, "Debug")

        relax_tab = QWidget()
        relax_form = make_form(relax_tab)

        self.wed_main = QCheckBox("Allow MAIN on Wednesday")
        self.wed_main.setChecked(settings.get("allow_post_weekend_wednesday_main"))
        self.wed_backup = QCheckBox("Allow BACKUP on Wednesday")
        self.wed_backup.setChecked(settings.get("allow_post_weekend_wednesday_backup"))
        self.thu_main = QCheckBox("Allow MAIN on Thursday")
        self.thu_main.setChecked(settings.get("allow_post_weekend_thursday_main"))
        self.thu_backup = QCheckBox("Allow BACKUP on Thursday")
        self.thu_backup.setChecked(settings.get("allow_post_weekend_thursday_backup"))

        self.hm_backup = WrappedCheck(
            "Allow Mon–Wed / Tue–Thu one-day gap (both BACKUP)",
            checked=settings.get("allow_midweek_pair_backup_only"),
        )
        self.hm_mixed = WrappedCheck(
            "Allow Mon–Wed / Tue–Thu one-day gap (MAIN + BACKUP)",
            checked=settings.get("allow_midweek_pair_mixed"),
        )

        for cb in (self.wed_main, self.wed_backup, self.thu_main, self.thu_backup):
            relax_form.addRow("", cb)
        relax_form.addRow("", self.hm_backup)
        relax_form.addRow("", self.hm_mixed)

        self.one_day_gap = WrappedCheck(
            "Enable one-day weekday gap fallback (Mon–Wed / Tue–Thu) outside pre/post-weekend",
            checked=settings.get("allow_one_day_weekday_gap"),
        )
        relax_form.addRow("", self.one_day_gap)

        self.tabs.addTab(relax_tab, "Relaxation")

        self.setLayout(body)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        if getattr(self, "is_android", False):
            for b in btns.buttons():
                b.setMinimumHeight(48)
        self._main_layout.addWidget(btns)

        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.viewport().installEventFilter(self)

        self.resize(560, 680)

    def showEvent(self, e):
        super().showEvent(e)
        scr = (
            self.windowHandle().screen()
            if self.windowHandle() and self.windowHandle().screen()
            else _AppScreen.primaryScreen()
        )
        if scr:
            geo = scr.availableGeometry()
            max_h = int(geo.height() * 0.85)
            max_w = int(geo.width() * 0.95)
            self.setMaximumSize(max_w, max_h)
            self.resize(min(self.width(), max_w), min(self.height(), max_h))
        QTimer.singleShot(0, self._sync_body_width)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._sync_body_width)

    def eventFilter(self, obj, ev):
        if obj is self._scroll.viewport() and ev.type() == QEvent.Resize:
            self._sync_body_width()
        return super().eventFilter(obj, ev)

    def _sync_body_width(self):
        """Force scroll-body, tabs and wrapped check texts to match viewport width."""
        if not self._scroll or not self._body:
            return
        vw = self._scroll.viewport().width()
        if vw <= 0:
            return
        self._body.setMinimumWidth(vw)
        self._body.setMaximumWidth(vw)
        self.tabs.setMaximumWidth(vw)
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            page.setMinimumWidth(0)
            page.setMaximumWidth(vw)
        wrap_width = vw - 60
        if hasattr(self, "hm_backup"):
            self.hm_backup.set_wrap_width(wrap_width)
        if hasattr(self, "hm_mixed"):
            self.hm_mixed.set_wrap_width(wrap_width)
        if hasattr(self, "one_day_gap"):
            self.one_day_gap.set_wrap_width(wrap_width)

    def values(self) -> dict:
        return {
            "theme": self.theme_combo.currentText(),
            "font_size": self.font_spin.value(),
            "accent_color": self.accent_edit.text(),
            "show_gif": self.gif_chk.isChecked(),
            "calendar_grid": self.grid_chk.isChecked(),
            "weekend_gap_days": self.weekend_gap.value(),
            "min_days_between_assignments": self.min_between.value(),
            "main_score_factor": self.main_factor.value(),
            "backup_score_factor": self.backup_factor.value(),
            "availability_penalty": self.avail_penalty.value(),
            "history_window_days": self.hist_window.value(),
            "history_duration_months": self.hist_duration.value(),
            "measure_phase_times": self.measure_chk.isChecked(),
            "analyse_initial_weekday_gaps": self.analyse_chk.isChecked(),
            "gap_report_file": self.gap_file.text(),
            "assignment_debug_enabled": self.assignment_debug_chk.isChecked(),
            "debug_variant_logging": self.debug_mode_combo.currentData() or "off",
            "allow_post_weekend_wednesday_main": self.wed_main.isChecked(),
            "allow_post_weekend_wednesday_backup": self.wed_backup.isChecked(),
            "allow_post_weekend_thursday_main": self.thu_main.isChecked(),
            "allow_post_weekend_thursday_backup": self.thu_backup.isChecked(),
            "allow_midweek_pair_backup_only": self.hm_backup.isChecked(),
            "allow_midweek_pair_mixed": self.hm_mixed.isChecked(),
            "allow_one_day_weekday_gap": self.one_day_gap.isChecked(),
        }


__all__ = ["CompactSettingsDialog"]
