"""Top-level application window and screen navigation."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QHeaderView,
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QWidget,
)

from scheduler import SharedSettings, build_scheduler_service

from .config import APP_TITLE, DB_NAME
from .dialogs import CompactSettingsDialog, SettingsDialog, ToolDialog
from .messages import show_error
from .platform import apply_backend_debug_preferences, is_android_platform
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
from .settings import AppSettings
from .style import UiStyle
from .theme import shade_color
from .widgets.date_pickers import MultiDatePicker, SingleDatePicker

logger = logging.getLogger(__name__)


class App(QMainWindow):
    """Top-level window that hosts the stacked screens."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(720, 560)

        # 1) persistent user settings -------------------------------------------------
        self.settings = AppSettings()

        # 2) apply current style *before* building widgets (important for Android) ---
        UiStyle.apply(
            QApplication.instance(),
            theme=self.settings.get("theme"),  # can be "dark" / "light" / "pink"
            accent_color=self.settings.get("accent_color"),
        )

        # 3) stacked container --------------------------------------------------------
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)

        self.backend = build_scheduler_service(DB_NAME)  # SchedulerService composition

        self._pages: list[tuple[str, type[QWidget]]] = [
            ("main", MainMenu),
            ("manage", NurseManagementScreen),
            ("prescheduled", PreScheduledScreen),
            ("assignment_hist", AssignmentHistoryScreen),
            ("view_unavail", ViewAllUnavailableScreen),
            ("weekend_history", WeekendHistoryCalendarScreen),
            ("generate", ScheduleGenerationScreen),
            ("advanced_stats", AdvancedWeekendStatsScreen),
        ]

        # 5) instantiate & register each page -----------------------------------------
        for name, cls in self._pages:
            page = cls(self)
            self.stack.addWidget(page)
            setattr(self, name, page)

        # 6) show main menu
        self.switch_frame("main")

        # 7) final style tweaks (font size, GIF toggle, etc.)
        self.apply_settings()
        apply_backend_debug_preferences(self.settings)

    # ─────────────────────────── navigation helper ───────────────────────────
    def switch_frame(self, name: str):
        """Switch to a named page (see self._pages for valid names)."""
        page_names = [n for n, _ in self._pages]
        if name not in page_names:
            raise ValueError(f"Unknown page: {name}")
        page = self.stack.widget(page_names.index(name))
        # Screens are built once, so each one reloads its data on entry;
        # otherwise edits made on one screen are invisible on the others.
        on_show = getattr(page, "on_show", None)
        if callable(on_show):
            try:
                on_show()
            except Exception:
                logger.exception("Refreshing screen %r failed", name)
        self.stack.setCurrentWidget(page)
        page.setFocus(Qt.OtherFocusReason)

    # ─────────────────────────── settings dialog ─────────────────────────────
    def open_settings_dialog(self):
        # Pick compact vs. desktop dialog at runtime
        if is_android_platform():
            dlg = CompactSettingsDialog(self.settings, self)
        else:
            dlg = SettingsDialog(self.settings, self)

        dlg.open()
        dlg.accepted.connect(lambda: self._apply_new_settings(dlg.values()))

    def _apply_new_settings(self, vals: dict) -> None:
        """
        Persist settings atomically and re-apply UI/Theme. Also refresh the
        backend's SharedSettings snapshot so any legacy code reading it gets
        the latest values.
        """
        try:
            self.settings.bulk_set(vals or {})
        except Exception as e:
            logger.exception("Saving settings failed")
            show_error(
                self,
                "Settings not saved",
                "Your changes apply to this session but could not be written to disk, "
                "so they will be lost when the app closes.",
                details=str(e),
            )
        self.apply_settings()
        apply_backend_debug_preferences(self.settings)

        # If any code in the backend still reads SharedSettings directly,
        # refresh its snapshot so it sees the newly written settings.json.
        try:
            # SchedulerService instance kept in self.backend
            # It created self.settings = SharedSettings() at init time.
            # Replace it with a fresh snapshot.
            self.backend.settings = SharedSettings()
        except Exception:
            pass

    def apply_settings(self) -> None:
        """
        Re-apply font size, palette/QSS, accent colour and live-refresh every
        open widget.  Runs only on the GUI thread (safe for Android).
        """
        app = QApplication.instance()

        # ── 1 · pull settings once
        font_size = self.settings.get("font_size")
        theme = self.settings.get("theme")
        accent = self.settings.get("accent_color")
        show_gif = self.settings.get("show_gif")
        show_grid = bool(self.settings.get("calendar_grid"))

        # ── 2 · global font
        app.setFont(QFont("Roboto", font_size))

        # ── 3 · palette + QSS
        UiStyle.apply(app, theme=theme, accent_color=accent)

        # helper ensures white-on-accent wherever Qt falls back to palette
        def _fix_selection_contrast(w: QWidget, accent_hex: str) -> None:
            accent_hex = shade_color(accent_hex, 1.0)
            pal = w.palette()
            pal.setColor(QPalette.Highlight, QColor(accent_hex))
            pal.setColor(QPalette.HighlightedText, Qt.white)
            w.setPalette(pal)

        # ── 4 · AUTO SIZE TABLE/LIST VIEWS ───────────────────────────────
        table_views = self.findChildren(QTableWidget) + self.findChildren(QTableView)
        list_views = self.findChildren(QListWidget)

        for view in table_views + list_views:
            # A QCalendarWidget's day grid is a QTableView too; fixing its row
            # height squashes the month into a strip, so leave calendars alone.
            if _inside_calendar(view):
                continue
            # keep selections readable
            _fix_selection_contrast(view, accent)

            # ----- table-specific tweaks
            if isinstance(view, (QTableWidget, QTableView)):
                fm = view.fontMetrics()
                row_height = fm.height() + 12  # 3 px top + 3 px bottom

                # vertical header (row numbers / icons)
                vh = view.verticalHeader()
                vh.setMinimumSectionSize(row_height)
                vh.setDefaultSectionSize(row_height)
                vh.setSectionResizeMode(QHeaderView.Fixed)  # consistent everywhere

                # horizontal header (column captions)
                hh = view.horizontalHeader()
                view.resizeColumnsToContents()  # natural size first
                hh.setStretchLastSection(True)  # fill remaining space
                hh.setMinimumSectionSize(40)  # never collapse too far

                # nicer look for tall rows on mobile
                hh.setFixedHeight(row_height + 2)

                # ── 5 · refresh any open ToolDialogs to new accent/theme
        accent = app.palette().color(QPalette.Highlight).name()  # <<<
        for dlg in self.findChildren(ToolDialog):
            dlg.refresh_accent(accent)  # recolour the border
            if hasattr(dlg, "apply_theme_update"):
                dlg.apply_theme_update()  # ── 6 · calendar pickers (multi + single)
        for picker in self.findChildren(MultiDatePicker):
            picker.set_theme(theme, accent, grid=show_grid)
        for picker in self.findChildren(SingleDatePicker):
            picker.set_theme(theme, accent, grid=show_grid)

        # ── 7 · main-menu GIF toggle
        if hasattr(self, "main"):
            self.main.update_gif(show_gif)

        # ── 8 · screen-level theme hooks
        for name, _cls in self._pages:
            page = getattr(self, name, None)
            if page and hasattr(page, "apply_theme_update"):
                page.apply_theme_update()


def _inside_calendar(widget: QWidget) -> bool:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QCalendarWidget):
            return True
        parent = parent.parentWidget()
    return False


__all__ = ["App", "APP_TITLE"]
