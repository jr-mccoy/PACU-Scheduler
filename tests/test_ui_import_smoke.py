"""Smoke tests for UI import wiring and compatibility wrappers."""

import pathlib
import re

from ui import app as app_wrapper
from ui import app_shell
from ui import workers as workers_wrapper
from ui import worker_threads
from ui.dialogs import (
    compact_settings_dialog,
    compact_settings_dialog_widget,
    rotation_violation_dialog,
    rotation_violation_dialog_widget,
    settings_dialog,
    settings_dialog_widget,
    tool_dialog,
    tool_dialog_widget,
    variant_review_dialog,
    variant_review_dialog_widget,
)
from ui.presenters import variant_review_presenter
from ui.screens import (
    advanced_weekend_stats,
    assignment_history,
    main_menu,
    nurse_management,
    nurse_management_screen,
    prescheduled,
    schedule_generation,
    schedule_generation_screen,
    view_all_unavailable,
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
    assert rotation_violation_dialog.RotationViolationDialog is rotation_violation_dialog_widget.RotationViolationDialog

    # The migrated dialogs must own the class — ui.legacy re-exports them.
    import ui.legacy as legacy

    assert legacy.ToolDialog is tool_dialog.ToolDialog
    assert legacy.SettingsDialog is settings_dialog.SettingsDialog
    assert legacy.CompactSettingsDialog is compact_settings_dialog.CompactSettingsDialog
    assert legacy.VariantReviewDialog is variant_review_dialog.VariantReviewDialog
    assert legacy.RotationViolationDialog is rotation_violation_dialog.RotationViolationDialog

    # The migrated screens must own their class — ui.legacy re-exports them.
    assert legacy.MainMenu is main_menu.MainMenu
    assert legacy.NurseManagementScreen is nurse_management.NurseManagementScreen
    assert legacy.PreScheduledScreen is prescheduled.PreScheduledScreen
    assert legacy.AssignmentHistoryScreen is assignment_history.AssignmentHistoryScreen
    assert legacy.ViewAllUnavailableScreen is view_all_unavailable.ViewAllUnavailableScreen
    assert legacy.ScheduleGenerationScreen is schedule_generation.ScheduleGenerationScreen
    assert legacy.WeekendHistoryCalendarScreen is weekend_history_calendar.WeekendHistoryCalendarScreen
    assert legacy.AdvancedWeekendStatsScreen is advanced_weekend_stats.AdvancedWeekendStatsScreen

    # And the new screen modules must own classes from their own files, not
    # subclass the legacy implementation (the previous wrapper-subclass pattern
    # is gone now that the screens live in their own modules).
    assert main_menu.MainMenu.__module__ == "ui.screens.main_menu"
    assert nurse_management.NurseManagementScreen.__module__ == "ui.screens.nurse_management"
    assert prescheduled.PreScheduledScreen.__module__ == "ui.screens.prescheduled"
    assert assignment_history.AssignmentHistoryScreen.__module__ == "ui.screens.assignment_history"
    assert view_all_unavailable.ViewAllUnavailableScreen.__module__ == "ui.screens.view_all_unavailable"
    assert schedule_generation.ScheduleGenerationScreen.__module__ == "ui.screens.schedule_generation"
    assert weekend_history_calendar.WeekendHistoryCalendarScreen.__module__ == "ui.screens.weekend_history_calendar"
    assert advanced_weekend_stats.AdvancedWeekendStatsScreen.__module__ == "ui.screens.advanced_weekend_stats"


def test_ui_modules_do_not_import_scheduler_legacy_core_directly():
    """``ui/**`` must reach the backend through the ``scheduler`` package facade.

    Importing ``scheduler.legacy_core`` directly defeats the facade boundary
    documented in ``scheduler/__init__.py``; this guard fails the build if any
    UI module re-introduces that coupling.
    """
    ui_root = pathlib.Path(__file__).resolve().parent.parent / "ui"
    pattern = re.compile(r"\bscheduler\.legacy_core\b")
    offenders = []
    for path in ui_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(ui_root.parent)))
    assert not offenders, (
        "UI modules must import from `scheduler`, not `scheduler.legacy_core`: "
        + ", ".join(offenders)
    )


def test_variant_presenter_and_export_services_are_importable():
    assert callable(variant_review_presenter._prepare_variant_debug_payload)
    assert callable(variant_review_presenter._build_html_for_top_variants)
    assert callable(variant_export._build_html_for_top_variants)
    assert callable(variant_export.export_variants_calendar_html)
    assert callable(variant_export.export_top_variants_pdfs)
