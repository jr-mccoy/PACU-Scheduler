"""Structured GUI package for the scheduler app."""

from .app import App
from .dialogs import CompactSettingsDialog, SettingsDialog, ToolDialog, VariantReviewDialog
from .screens import (
    NurseManagementScreen,
    ScheduleGenerationScreen,
    WeekendHistoryCalendarScreen,
)
from .services import (
    _build_html_for_top_variants,
    export_top_variants_pdfs,
    export_variants_calendar_html,
)
from .theme import (
    CAL_BORDER,
    _apply_header,
    _apply_pink_header,
    apply_theme_to_calendar,
    themed_file,
    themed_icon,
)
from .style import UiStyle
from .workers import RebuildViolationWorker, ScheduleProgressWorker

__all__ = [
    "App",
    "ScheduleGenerationScreen",
    "WeekendHistoryCalendarScreen",
    "NurseManagementScreen",
    "ToolDialog",
    "SettingsDialog",
    "CompactSettingsDialog",
    "VariantReviewDialog",
    "ScheduleProgressWorker",
    "RebuildViolationWorker",
    "CAL_BORDER",
    "themed_file",
    "themed_icon",
    "_apply_header",
    "_apply_pink_header",
    "apply_theme_to_calendar",
    "UiStyle",
    "_build_html_for_top_variants",
    "export_variants_calendar_html",
    "export_top_variants_pdfs",
]
