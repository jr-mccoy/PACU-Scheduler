"""Smoke tests for UI import wiring and compatibility wrappers."""

from ui import app as app_wrapper
from ui import app_shell
from ui import workers as workers_wrapper
from ui import worker_threads
from ui.dialogs import (
    compact_settings_dialog,
    compact_settings_dialog_widget,
    settings_dialog,
    settings_dialog_widget,
    tool_dialog,
    tool_dialog_widget,
    variant_review_dialog,
    variant_review_dialog_widget,
)
from ui.presenters import variant_review_presenter
from ui.screens import (
    nurse_management,
    nurse_management_screen,
    schedule_generation,
    schedule_generation_screen,
    weekend_history_calendar,
    weekend_history_calendar_screen,
)
from ui.services import variant_export


def test_ui_wrapper_modules_point_to_concrete_modules():
    assert app_wrapper.App is app_shell.App
    assert workers_wrapper.ScheduleProgressWorker is worker_threads.ScheduleProgressWorker
    assert workers_wrapper.RebuildViolationWorker is worker_threads.RebuildViolationWorker

    assert schedule_generation.ScheduleGenerationScreen is schedule_generation_screen.ScheduleGenerationScreen
    assert nurse_management.NurseManagementScreen is nurse_management_screen.NurseManagementScreen
    assert weekend_history_calendar.WeekendHistoryCalendarScreen is weekend_history_calendar_screen.WeekendHistoryCalendarScreen

    assert tool_dialog.ToolDialog is tool_dialog_widget.ToolDialog
    assert settings_dialog.SettingsDialog is settings_dialog_widget.SettingsDialog
    assert compact_settings_dialog.CompactSettingsDialog is compact_settings_dialog_widget.CompactSettingsDialog
    assert variant_review_dialog.VariantReviewDialog is variant_review_dialog_widget.VariantReviewDialog


def test_variant_presenter_and_export_services_are_importable():
    assert callable(variant_review_presenter._prepare_variant_debug_payload)
    assert callable(variant_review_presenter._build_html_for_top_variants)
    assert callable(variant_export._build_html_for_top_variants)
    assert callable(variant_export.export_variants_calendar_html)
    assert callable(variant_export.export_top_variants_pdfs)
