"""Help text, defaults and validation shared by both settings dialogs.

``SettingsDialog`` (desktop) and ``CompactSettingsDialog`` (phones) lay the
same settings out differently but name their widgets identically, so the
code that explains, resets and validates them lives here once.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLineEdit,
    QSpinBox,
)

from ..settings import AppSettings

ACCENT_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# settings key -> widget attribute name on either dialog.
#
# main_score_factor, backup_score_factor and availability_penalty are
# deliberately absent: the scheduler has never read them, so the dialogs no
# longer offer them. Saved values are left untouched in settings.json.
WIDGET_FOR_KEY = {
    "theme": "theme_combo",
    "font_size": "font_spin",
    "accent_color": "accent_edit",
    "show_gif": "gif_chk",
    "calendar_grid": "grid_chk",
    "weekend_gap_days": "weekend_gap",
    "min_days_between_assignments": "min_between",
    "history_window_days": "hist_window",
    "history_duration_months": "hist_duration",
    "max_weekend_variants": "variant_cap",
    "measure_phase_times": "measure_chk",
    "analyse_initial_weekday_gaps": "analyse_chk",
    "gap_report_file": "gap_file",
    "assignment_debug_enabled": "assignment_debug_chk",
    "debug_variant_logging": "debug_mode_combo",
    "allow_post_weekend_wednesday_main": "wed_main",
    "allow_post_weekend_wednesday_backup": "wed_backup",
    "allow_post_weekend_thursday_main": "thu_main",
    "allow_post_weekend_thursday_backup": "thu_backup",
    "allow_midweek_pair_backup_only": "hm_backup",
    "allow_midweek_pair_mixed": "hm_mixed",
    "allow_one_day_weekday_gap": "one_day_gap",
}

# (scoring_weights key, widget attribute, label, tooltip)
# Rotation repeats and unfilled slots have no weight: options are always
# ranked by them first (scheduler.scoring.RANK_FIRST). See WEIGHTS_HELP.
WEIGHTS = [
    (
        "rot_viol",
        "w_rotviol",
        "Past violations:",
        "Among options with the same number of rotation repeats, favour those that give "
        "the repeats to nurses with fewer past violations.",
    ),
    (
        "weekend_gap",
        "w_wgap",
        "Weekend spacing:",
        "Favour giving weekends to nurses whose last weekend was longest ago.",
    ),
    (
        "balance",
        "w_balance",
        "Balance:",
        "Penalise uneven Main/Backup counts across nurses within the schedule.",
    ),
    (
        "long_term",
        "w_longterm",
        "Long-term fairness:",
        "Penalise giving more shifts to nurses who worked more in the recent history window.",
    ),
]

WEIGHTS_HELP = (
    "Options are ranked by fewest rotation repeats, then fewest unfilled slots. "
    "These weights order the options that tie on both. Only the ratios matter."
)

LABELS = {
    "allow_post_weekend_wednesday_main": "Main on the Wednesday after a worked weekend",
    "allow_post_weekend_wednesday_backup": "Backup on the Wednesday after a worked weekend",
    "allow_post_weekend_thursday_main": "Main on the Thursday after a worked weekend",
    "allow_post_weekend_thursday_backup": "Backup on the Thursday after a worked weekend",
    "allow_one_day_weekday_gap": "Any roles (Mon–Wed or Tue–Thu)",
    "allow_midweek_pair_backup_only": "Only when both shifts are Backup",
    "allow_midweek_pair_mixed": "Only when one shift is Main and the other Backup",
}

TOOLTIPS = {
    "theme": "Colour scheme for the whole app.",
    "font_size": "Base text size in points.",
    "accent_color": "Highlight colour as #RRGGBB, e.g. #5C8DBC.",
    "show_gif": "Play GIF.gif on the main menu when the file is present.",
    "calendar_grid": "Draw lines between days in every calendar.",
    "weekend_gap_days": (
        "Hard rule: the fewest days between the Fridays of two weekends one nurse works. "
        "Weekends exactly this far apart are allowed (28 = every fourth weekend at most)."
    ),
    "min_days_between_assignments": (
        "Days a nurse must have off between two weekday shifts. The one-day gap "
        "options below can relax this by a day when nobody else can work."
    ),
    "history_window_days": (
        "Days of assignment history, ending the day before the start date, "
        "counted for long-term fairness."
    ),
    "history_duration_months": (
        "How many months of assignment history to load at all. The fairness "
        "window above can only look back this far."
    ),
    "max_weekend_variants": (
        "Weekend combinations kept after each weekend and fully evaluated.\n"
        "Higher explores more candidate schedules; run time grows roughly\n"
        "in proportion. Unlimited can take hours on long horizons."
    ),
    "measure_phase_times": (
        "Time each evaluation phase and save performance_metrics.json next to the exported PDFs."
    ),
    "analyse_initial_weekday_gaps": (
        "Save a report of empty weekday slots per option (before and after repair) "
        "next to the exported PDFs."
    ),
    "gap_report_file": (
        "File name for the gap report. A plain name is saved in the export "
        "folder; an absolute path is used as-is."
    ),
    "assignment_debug_enabled": (
        "Write assignment_debug_*.jsonl/.csv traces to the working folder. Verbose."
    ),
    "debug_variant_logging": "Extra console logging while weekend variants are built.",
    "allow_post_weekend_wednesday_main": (
        "Let a nurse who worked the weekend take Main on the following Wednesday."
    ),
    "allow_post_weekend_wednesday_backup": (
        "Let a nurse who worked the weekend take Backup on the following Wednesday."
    ),
    "allow_post_weekend_thursday_main": (
        "Let a nurse who worked the weekend take Main on the following Thursday."
    ),
    "allow_post_weekend_thursday_backup": (
        "Let a nurse who worked the weekend take Backup on the following Thursday."
    ),
    "allow_one_day_weekday_gap": (
        "When no nurse is otherwise eligible, allow one day off between weekday "
        "shifts (Mon–Wed or Tue–Thu) whatever the roles. Never next to a nurse's weekend."
    ),
    "allow_midweek_pair_backup_only": (
        "Like the option above, but only when both shifts are Backup."
    ),
    "allow_midweek_pair_mixed": (
        "Like the option above, but only when one shift is Main and the other Backup."
    ),
}

ONE_DAY_GAP_HELP = (
    "Used only when no nurse can otherwise fill a Mon–Thu slot, and never next to a "
    "nurse's own weekend. Tick “Any roles”, or narrow it to the role pairs below."
)


def is_valid_accent(text: str) -> bool:
    return bool(ACCENT_RE.match(text.strip()))


def _set_value(widget, value) -> None:
    if widget is None or value is None:
        return
    if isinstance(widget, QComboBox):
        idx = widget.findData(value)
        if idx < 0:
            idx = widget.findText(str(value))
        if idx >= 0:
            widget.setCurrentIndex(idx)
    elif isinstance(widget, QDoubleSpinBox):
        widget.setValue(float(value))
    elif isinstance(widget, QSpinBox):
        widget.setValue(int(value))
    elif isinstance(widget, QLineEdit):
        widget.setText(str(value))
    elif hasattr(widget, "setChecked"):
        widget.setChecked(bool(value))


def load_values(dialog, values: dict) -> None:
    """Show *values* (a settings mapping) in *dialog*'s widgets."""
    for key, attr in WIDGET_FOR_KEY.items():
        if key in values:
            _set_value(getattr(dialog, attr, None), values[key])
    weights = values.get("scoring_weights") or {}
    for key, attr, _label, _tip in WEIGHTS:
        if key in weights:
            _set_value(getattr(dialog, attr, None), weights[key])


def weight_values(dialog) -> dict[str, float]:
    return {key: float(getattr(dialog, attr).value()) for key, attr, _l, _t in WEIGHTS}


def apply_tooltips(dialog) -> None:
    for key, attr in WIDGET_FOR_KEY.items():
        widget = getattr(dialog, attr, None)
        tip = TOOLTIPS.get(key)
        if widget is not None and tip:
            widget.setToolTip(tip)
    for _key, attr, _label, tip in WEIGHTS:
        widget = getattr(dialog, attr, None)
        if widget is not None:
            widget.setToolTip(tip)


def add_restore_defaults(dialog, buttons: QDialogButtonBox) -> QAbstractButton:
    """Add a Restore Defaults button that resets the form (not the saved file)."""
    btn = buttons.addButton(QDialogButtonBox.RestoreDefaults)
    btn.setToolTip("Put every field back to its default. Nothing is saved until you click Save.")
    btn.setAutoDefault(False)
    btn.clicked.connect(lambda: load_values(dialog, AppSettings.DEFAULTS))
    return btn


def validate_and_accept(dialog) -> None:
    """Accept *dialog* unless a field is invalid, in which case say which."""
    from ..messages import show_warning

    accent = dialog.accent_edit.text().strip()
    if not is_valid_accent(accent):
        show_warning(
            dialog,
            "Check the accent colour",
            f"“{accent}” is not a colour. Use the #RRGGBB form, for example #5C8DBC.",
        )
        dialog.accent_edit.setFocus()
        return
    dialog.accent_edit.setText(accent.upper())
    dialog.accept()


__all__ = [
    "LABELS",
    "ONE_DAY_GAP_HELP",
    "TOOLTIPS",
    "WEIGHTS",
    "WIDGET_FOR_KEY",
    "add_restore_defaults",
    "apply_tooltips",
    "is_valid_accent",
    "load_values",
    "validate_and_accept",
    "weight_values",
]
