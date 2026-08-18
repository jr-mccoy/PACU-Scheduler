"""Desktop Settings dialog — full-form settings editor."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .tool_dialog import ToolDialog


class SettingsDialog(ToolDialog):
    """Desktop Settings dialog with a ``values()`` method.

    Includes the post-weekend relaxation options and the midweek one-day
    gap toggles.
    """

    def __init__(self, settings, parent=None):
        super().__init__(parent, title="Settings")
        self.settings = settings

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        shell = QWidget()
        scroll.setWidget(shell)
        main_layout = QVBoxLayout(shell)
        main_layout.setContentsMargins(12, 12, 18, 12)
        main_layout.setSpacing(18)

        ui_group = QGroupBox("UI")
        ui_layout = QGridLayout()
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light", "pink"])
        self.theme_combo.setCurrentText(settings.get("theme"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 32)
        self.font_spin.setValue(settings.get("font_size"))
        self.accent_edit = QLineEdit(settings.get("accent_color"))
        self.gif_chk = QCheckBox("Show animated GIF")
        self.gif_chk.setChecked(settings.get("show_gif"))
        self.grid_chk = QCheckBox("Show calendar grid")
        self.grid_chk.setChecked(settings.get("calendar_grid"))
        r = 0
        for label, w in [
            ("Theme:", self.theme_combo),
            ("Font size:", self.font_spin),
            ("Accent color:", self.accent_edit),
        ]:
            ui_layout.addWidget(QLabel(label), r, 0)
            ui_layout.addWidget(w, r, 1)
            r += 1
        ui_layout.addWidget(self.gif_chk, r, 0, 1, 2)
        r += 1
        ui_layout.addWidget(self.grid_chk, r, 0, 1, 2)
        ui_group.setLayout(ui_layout)
        main_layout.addWidget(ui_group)

        sched_group = QGroupBox("Scheduling")
        sched_layout = QGridLayout()

        def sb(val, lo, hi):
            w = QSpinBox()
            w.setRange(lo, hi)
            w.setValue(val)
            return w

        self.weekend_gap = sb(settings.get("weekend_gap_days"), 7, 90)
        self.min_between = sb(settings.get("min_days_between_assignments"), 0, 7)
        self.main_factor = sb(settings.get("main_score_factor"), 1, 100)
        self.backup_factor = sb(settings.get("backup_score_factor"), 1, 100)
        self.avail_penalty = sb(settings.get("availability_penalty"), 0, 100)
        self.hist_window = sb(settings.get("history_window_days"), 1, 365)
        self.hist_duration = sb(settings.get("history_duration_months"), 1, 60)
        r = 0
        for label, w in [
            ("Weekend gap (days):", self.weekend_gap),
            ("Min days between:", self.min_between),
            ("Main score factor:", self.main_factor),
            ("Backup score factor:", self.backup_factor),
            ("Availability penalty:", self.avail_penalty),
            ("History window (days):", self.hist_window),
            ("History duration (months):", self.hist_duration),
        ]:
            sched_layout.addWidget(QLabel(label), r, 0)
            sched_layout.addWidget(w, r, 1)
            r += 1
        sched_group.setLayout(sched_layout)
        main_layout.addWidget(sched_group)

        debug_group = QGroupBox("Debug / Diagnostics")
        debug_layout = QGridLayout()
        self.measure_chk = QCheckBox("Measure phase times")
        self.measure_chk.setChecked(settings.get("measure_phase_times"))
        self.analyse_chk = QCheckBox("Analyse initial gaps")
        self.analyse_chk.setChecked(settings.get("analyse_initial_weekday_gaps"))
        self.assignment_debug_chk = QCheckBox("Enable structured assignment debug logging")
        self.assignment_debug_chk.setChecked(settings.get("assignment_debug_enabled"))
        self.debug_mode_combo = QComboBox()
        self.debug_mode_combo.addItem("Off", "off")
        self.debug_mode_combo.addItem("Pairs only", "pairs")
        self.debug_mode_combo.addItem("Variants only", "variants")
        self.debug_mode_combo.addItem("Pairs + variants", "all")
        dbg_idx = self.debug_mode_combo.findData(settings.get("debug_variant_logging"))
        if dbg_idx < 0:
            dbg_idx = 0
        self.debug_mode_combo.setCurrentIndex(dbg_idx)
        self.gap_file = QLineEdit(settings.get("gap_report_file"))
        rows = [
            (self.measure_chk, None),
            (self.analyse_chk, None),
            (self.assignment_debug_chk, None),
            (QLabel("Variant debug:"), self.debug_mode_combo),
            (QLabel("Gap report file:"), self.gap_file),
        ]
        r = 0
        for left, right in rows:
            if right is None:
                debug_layout.addWidget(left, r, 0, 1, 2)
            else:
                debug_layout.addWidget(left, r, 0)
                debug_layout.addWidget(right, r, 1)
            r += 1
        debug_group.setLayout(debug_layout)
        main_layout.addWidget(debug_group)

        relax_group = QGroupBox("Post-Weekend Relaxations")
        relax_layout = QGridLayout()
        self.wed_main = QCheckBox("Allow MAIN on Wednesday")
        self.wed_main.setChecked(settings.get("allow_post_weekend_wednesday_main"))
        self.wed_backup = QCheckBox("Allow BACKUP on Wednesday")
        self.wed_backup.setChecked(settings.get("allow_post_weekend_wednesday_backup"))
        self.thu_main = QCheckBox("Allow MAIN on Thursday")
        self.thu_main.setChecked(settings.get("allow_post_weekend_thursday_main"))
        self.thu_backup = QCheckBox("Allow BACKUP on Thursday")
        self.thu_backup.setChecked(settings.get("allow_post_weekend_thursday_backup"))

        self.hm_backup = QCheckBox("Allow Mon–Wed / Tue–Thu one-day gap (both BACKUP)")
        self.hm_backup.setChecked(settings.get("allow_midweek_pair_backup_only"))
        self.hm_mixed = QCheckBox("Allow Mon–Wed / Tue–Thu one-day gap (MAIN + BACKUP)")
        self.hm_mixed.setChecked(settings.get("allow_midweek_pair_mixed"))

        self.one_day_gap = QCheckBox("Enable one-day weekday gap fallback (Mon–Wed / Tue–Thu)")
        self.one_day_gap.setChecked(settings.get("allow_one_day_weekday_gap"))

        for i, w in enumerate(
            [
                self.wed_main,
                self.wed_backup,
                self.thu_main,
                self.thu_backup,
                self.hm_backup,
                self.hm_mixed,
                self.one_day_gap,
            ]
        ):
            relax_layout.addWidget(w, i, 0, 1, 2)
        relax_group.setLayout(relax_layout)
        main_layout.addWidget(relax_group)

        score_group = QGroupBox("Scoring Weights (normalized internally)")
        score_layout = QGridLayout()
        wdict = settings.get("scoring_weights")

        def wspin(val):
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(0.0, 1.0)
            w.setSingleStep(0.05)
            w.setValue(float(val))
            return w

        self.w_rotrep = wspin(wdict.get("rotation_rep", 0.30))
        self.w_gaps = wspin(wdict.get("gaps", 0.20))
        self.w_rotviol = wspin(wdict.get("rot_viol", 0.15))
        self.w_wgap = wspin(wdict.get("weekend_gap", 0.15))
        self.w_balance = wspin(wdict.get("balance", 0.10))
        self.w_longterm = wspin(wdict.get("long_term", 0.10))
        labels = [
            "Rotation repeats:",
            "Weekday gaps:",
            "Rotation violations:",
            "Weekend gap:",
            "Balance:",
            "Long-term:",
        ]
        spins = [
            self.w_rotrep,
            self.w_gaps,
            self.w_rotviol,
            self.w_wgap,
            self.w_balance,
            self.w_longterm,
        ]
        for i, (lab, sp) in enumerate(zip(labels, spins, strict=True)):
            score_layout.addWidget(QLabel(lab), i, 0)
            score_layout.addWidget(sp, i, 1)
        score_group.setLayout(score_layout)
        main_layout.addWidget(score_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)

        outer = QVBoxLayout()
        outer.addWidget(scroll)
        self.setLayout(outer)
        self.resize(600, 780)

    def values(self) -> dict:
        """Return all updated settings from this dialog."""
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
            "scoring_weights": {
                "rotation_rep": float(self.w_rotrep.value()),
                "gaps": float(self.w_gaps.value()),
                "rot_viol": float(self.w_rotviol.value()),
                "weekend_gap": float(self.w_wgap.value()),
                "balance": float(self.w_balance.value()),
                "long_term": float(self.w_longterm.value()),
            },
        }


__all__ = ["SettingsDialog"]
