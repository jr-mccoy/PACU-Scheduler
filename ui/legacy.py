"""Deprecated compatibility facade for UI symbols moved to focused modules."""

from .app_shell import App
from .config import CAL_BORDER, DB_NAME, DEBUG_SAVE_VARIANTS
from .dialogs import (
    CompactSettingsDialog,
    RotationViolationDialog,
    SettingsDialog,
    ToolDialog,
    VariantReviewDialog,
)
from .messages import confirm, show_error, show_info, show_warning
from .platform import (
    _open_external,
    _runtime_base_dir,
    adjust_dialog_for_android,
    apply_backend_debug_preferences,
    is_android_platform,
    tune_dialog,
)
from .presenters.variant_review_presenter import (
    _build_html_for_top_variants,
    _prepare_variant_debug_payload,
)
from .screens import (
    AdvancedWeekendStatsScreen,
    AssignmentHistoryScreen,
    MainMenu,
    NurseManagementScreen,
    PreScheduledScreen,
    ScheduleGenerationScreen,
    ViewAllUnavailableScreen,
    WeekendHistoryCalendarScreen,
)
from .services.variant_export import (
    _save_outputs_for_variants,
    export_top_variants_pdfs,
    export_variants_calendar_html,
)
from .settings import AppSettings
from .style import UiStyle
from .theme import (
    _apply_header,
    _apply_pink_header,
    apply_theme_to_calendar,
    shade_color,
    themed_file,
    themed_icon,
)
from .worker_threads import RebuildViolationWorker, ScheduleProgressWorker
from .widgets.common import ConfirmOverlay, WrapDelegate, WrappedCheck
from .widgets.date_pickers import MultiDatePicker, MultiDatePickerGrid, SingleDatePicker
from .widgets.header_views import MultiLineHeaderView, PinkHeaderView, TallHeaderView

__all__ = [name for name in globals() if not name.startswith("__")]
