"""Desktop Settings dialog — full-form settings editor."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from scheduler import MAX_WEEKEND_VARIANTS_RANGE

from .settings_support import (
    LABELS,
    ONE_DAY_GAP_HELP,
    WEIGHTS,
    WEIGHTS_HELP,
    add_restore_defaults,
    apply_tooltips,
    is_valid_accent,
    load_values,
    validate_and_accept,
    weight_values,
)
from .tool_dialog import ToolDialog


def _help(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setProperty("role", "muted")
    return lbl


class SettingsDialog(ToolDialog):
    """Desktop Settings dialog with a ``values()`` method.

    Hover any field for what it does. Save/Cancel/Restore Defaults stay
    pinned below the scrolling form.
    """

    def __init__(self, settings, parent=None):
        super().__init__(parent, title="Settings")
        self.settings = settings

        shell = QWidget()
        main_layout = QVBoxLayout(shell)
        main_layout.setContentsMargins(4, 4, 12, 4)
        main_layout.setSpacing(18)

        # ── Appearance ────────────────────────────────────────────────
        ui_group = QGroupBox("Appearance")
        ui_layout = QGridLayout()
        self.theme_combo = QComboBox()
        for label, value in (("Dark", "dark"), ("Light", "light"), ("Pink", "pink")):
            self.theme_combo.addItem(label, value)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 32)
        self.font_spin.setSuffix(" pt")
        self.accent_edit = QLineEdit()
        self.accent_edit.setPlaceholderText("#5C8DBC")
        self.accent_edit.setMaxLength(7)
        self.accent_swatch = QPushButton()
        self.accent_swatch.setFixedSize(36, 28)
        self.accent_swatch.setToolTip("Pick a colour")
        self.accent_swatch.setAccessibleName("Pick accent colour")
        self.accent_swatch.setAutoDefault(False)
        self.accent_swatch.clicked.connect(self._pick_accent)
        self.accent_edit.textChanged.connect(self._update_swatch)
        accent_row = QHBoxLayout()
        accent_row.addWidget(self.accent_edit, 1)
        accent_row.addWidget(self.accent_swatch)
        self.gif_chk = QCheckBox("Show animated GIF on the main menu")
        self.grid_chk = QCheckBox("Show calendar grid lines")
        ui_layout.addWidget(QLabel("Theme:"), 0, 0)
        ui_layout.addWidget(self.theme_combo, 0, 1)
        ui_layout.addWidget(QLabel("Font size:"), 1, 0)
        ui_layout.addWidget(self.font_spin, 1, 1)
        ui_layout.addWidget(QLabel("Accent colour:"), 2, 0)
        ui_layout.addLayout(accent_row, 2, 1)
        ui_layout.addWidget(self.gif_chk, 3, 0, 1, 2)
        ui_layout.addWidget(self.grid_chk, 4, 0, 1, 2)
        ui_layout.setColumnStretch(1, 1)
        ui_group.setLayout(ui_layout)
        main_layout.addWidget(ui_group)

        # ── Scheduling rules ──────────────────────────────────────────
        sched_group = QGroupBox("Scheduling rules")
        sched_layout = QGridLayout()

        def sb(lo, hi, suffix=""):
            w = QSpinBox()
            w.setRange(lo, hi)
            if suffix:
                w.setSuffix(suffix)
            return w

        self.weekend_gap = sb(7, 90, " days")
        self.min_between = sb(0, 7, " days")
        self.hist_window = sb(1, 365, " days")
        self.hist_duration = sb(1, 60, " months")
        self.variant_cap = sb(*MAX_WEEKEND_VARIANTS_RANGE)
        self.variant_cap.setSpecialValueText("Unlimited")
        self.variant_cap.setSingleStep(100)
        self.variant_cap.setGroupSeparatorShown(True)
        for r, (label, w) in enumerate(
            [
                ("Minimum gap between weekends:", self.weekend_gap),
                ("Days off between weekday shifts:", self.min_between),
                ("Fairness history window:", self.hist_window),
                ("History to load:", self.hist_duration),
                ("Weekend variants to evaluate:", self.variant_cap),
            ]
        ):
            sched_layout.addWidget(QLabel(label), r, 0)
            sched_layout.addWidget(w, r, 1)
        sched_layout.setColumnStretch(1, 1)
        sched_group.setLayout(sched_layout)
        main_layout.addWidget(sched_group)

        # ── Relaxations ───────────────────────────────────────────────
        relax_group = QGroupBox("After a worked weekend, allow…")
        relax_layout = QVBoxLayout()
        self.wed_main = QCheckBox(LABELS["allow_post_weekend_wednesday_main"])
        self.wed_backup = QCheckBox(LABELS["allow_post_weekend_wednesday_backup"])
        self.thu_main = QCheckBox(LABELS["allow_post_weekend_thursday_main"])
        self.thu_backup = QCheckBox(LABELS["allow_post_weekend_thursday_backup"])
        for w in (self.wed_main, self.wed_backup, self.thu_main, self.thu_backup):
            relax_layout.addWidget(w)
        relax_group.setLayout(relax_layout)
        main_layout.addWidget(relax_group)

        gap_group = QGroupBox("One-day gap between weekday shifts")
        gap_layout = QVBoxLayout()
        gap_layout.addWidget(_help(ONE_DAY_GAP_HELP))
        self.one_day_gap = QCheckBox(LABELS["allow_one_day_weekday_gap"])
        self.hm_backup = QCheckBox(LABELS["allow_midweek_pair_backup_only"])
        self.hm_mixed = QCheckBox(LABELS["allow_midweek_pair_mixed"])
        gap_layout.addWidget(self.one_day_gap)
        gap_layout.addWidget(self.hm_backup)
        gap_layout.addWidget(self.hm_mixed)
        # "Any roles" already covers both narrower options.
        self.one_day_gap.toggled.connect(lambda on: self.hm_backup.setEnabled(not on))
        self.one_day_gap.toggled.connect(lambda on: self.hm_mixed.setEnabled(not on))
        gap_group.setLayout(gap_layout)
        main_layout.addWidget(gap_group)

        # ── Ranking weights ───────────────────────────────────────────
        score_group = QGroupBox("Ranking weights")
        score_layout = QGridLayout()
        score_layout.addWidget(
            _help(WEIGHTS_HELP),
            0,
            0,
            1,
            2,
        )
        for i, (_key, attr, label, _tip) in enumerate(WEIGHTS, start=1):
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(0.0, 1.0)
            w.setSingleStep(0.05)
            setattr(self, attr, w)
            score_layout.addWidget(QLabel(label), i, 0)
            score_layout.addWidget(w, i, 1)
        score_layout.setColumnStretch(1, 1)
        score_group.setLayout(score_layout)
        main_layout.addWidget(score_group)

        # ── Diagnostics ───────────────────────────────────────────────
        debug_group = QGroupBox("Diagnostics")
        debug_layout = QGridLayout()
        self.measure_chk = QCheckBox("Save phase timings with each run")
        self.analyse_chk = QCheckBox("Save a weekday gap report with each run")
        self.gap_file = QLineEdit()
        self.gap_file.setPlaceholderText("weekday_gap_report.txt")
        self.analyse_chk.toggled.connect(self.gap_file.setEnabled)
        self.assignment_debug_chk = QCheckBox("Write assignment debug traces")
        self.debug_mode_combo = QComboBox()
        self.debug_mode_combo.addItem("Off", "off")
        self.debug_mode_combo.addItem("Pairs only", "pairs")
        self.debug_mode_combo.addItem("Variants only", "variants")
        self.debug_mode_combo.addItem("Pairs + variants", "all")
        debug_layout.addWidget(self.measure_chk, 0, 0, 1, 2)
        debug_layout.addWidget(self.analyse_chk, 1, 0, 1, 2)
        debug_layout.addWidget(QLabel("Gap report file:"), 2, 0)
        debug_layout.addWidget(self.gap_file, 2, 1)
        debug_layout.addWidget(self.assignment_debug_chk, 3, 0, 1, 2)
        debug_layout.addWidget(QLabel("Variant debug logging:"), 4, 0)
        debug_layout.addWidget(self.debug_mode_combo, 4, 1)
        debug_layout.setColumnStretch(1, 1)
        debug_group.setLayout(debug_layout)
        main_layout.addWidget(debug_group)
        main_layout.addStretch(1)

        # The form scrolls inside the card; the buttons stay pinned below it.
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        self.setLayout(outer)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        add_restore_defaults(self, buttons)
        buttons.accepted.connect(lambda: validate_and_accept(self))
        buttons.rejected.connect(self.reject)
        self._main_layout.addSpacing(12)
        self._main_layout.addWidget(buttons)

        load_values(self, settings.all() if hasattr(settings, "all") else dict(settings))
        apply_tooltips(self)
        self.gap_file.setEnabled(self.analyse_chk.isChecked())
        self._update_swatch(self.accent_edit.text())
        self.resize(640, 800)

    # ─────────────────────────── accent colour ───────────────────────────
    def _update_swatch(self, text: str) -> None:
        colour = text.strip() if is_valid_accent(text) else "transparent"
        self.accent_swatch.setStyleSheet(
            f"QPushButton {{ background:{colour}; border:2px solid palette(mid);"
            " border-radius:6px; padding:0; }"
        )

    def _pick_accent(self) -> None:
        current = QColor(self.accent_edit.text().strip())
        chosen = QColorDialog.getColor(current if current.isValid() else QColor("#5C8DBC"), self)
        if chosen.isValid():
            self.accent_edit.setText(chosen.name().upper())

    def values(self) -> dict:
        """Return all updated settings from this dialog."""
        return {
            "theme": self.theme_combo.currentData() or self.theme_combo.currentText(),
            "font_size": self.font_spin.value(),
            "accent_color": self.accent_edit.text().strip(),
            "show_gif": self.gif_chk.isChecked(),
            "calendar_grid": self.grid_chk.isChecked(),
            "weekend_gap_days": self.weekend_gap.value(),
            "min_days_between_assignments": self.min_between.value(),
            "history_window_days": self.hist_window.value(),
            "history_duration_months": self.hist_duration.value(),
            "max_weekend_variants": self.variant_cap.value(),
            "measure_phase_times": self.measure_chk.isChecked(),
            "analyse_initial_weekday_gaps": self.analyse_chk.isChecked(),
            "gap_report_file": self.gap_file.text().strip() or "weekday_gap_report.txt",
            "assignment_debug_enabled": self.assignment_debug_chk.isChecked(),
            "debug_variant_logging": self.debug_mode_combo.currentData() or "off",
            "allow_post_weekend_wednesday_main": self.wed_main.isChecked(),
            "allow_post_weekend_wednesday_backup": self.wed_backup.isChecked(),
            "allow_post_weekend_thursday_main": self.thu_main.isChecked(),
            "allow_post_weekend_thursday_backup": self.thu_backup.isChecked(),
            "allow_midweek_pair_backup_only": self.hm_backup.isChecked(),
            "allow_midweek_pair_mixed": self.hm_mixed.isChecked(),
            "allow_one_day_weekday_gap": self.one_day_gap.isChecked(),
            "scoring_weights": weight_values(self),
        }


__all__ = ["SettingsDialog"]
