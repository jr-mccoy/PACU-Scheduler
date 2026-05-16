"""Top-level exports for GUI screens."""

from .advanced_weekend_stats import AdvancedWeekendStatsScreen
from .assignment_history import AssignmentHistoryScreen
from .main_menu import MainMenu
from .nurse_management import NurseManagementScreen
from .prescheduled import PreScheduledScreen
from .schedule_generation import ScheduleGenerationScreen
from .view_all_unavailable import ViewAllUnavailableScreen
from .weekend_history_calendar import WeekendHistoryCalendarScreen

__all__ = [
    "AdvancedWeekendStatsScreen",
    "AssignmentHistoryScreen",
    "MainMenu",
    "NurseManagementScreen",
    "PreScheduledScreen",
    "ScheduleGenerationScreen",
    "ViewAllUnavailableScreen",
    "WeekendHistoryCalendarScreen",
]
