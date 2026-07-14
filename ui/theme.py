"""Theme color math, icons, calendars, and selection contrast."""

from __future__ import annotations

import os
import warnings
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QCalendarWidget, QHeaderView, QTableView, QWidget

try:  # Also supports direct file loading by lightweight theme tests/tools.
    from .config import CAL_BORDER
except ImportError:  # pragma: no cover - direct module execution
    CAL_BORDER = "#E9A9B8"

if TYPE_CHECKING:
    from .style import UiStyle

def shade_color(hex_rgb: str, factor: float) -> str:
    """Scale an RGB hex color and clamp every component to 0..255."""
    if not isinstance(hex_rgb, str) or not hex_rgb.startswith("#") or len(hex_rgb) != 7:
        return hex_rgb
    value = hex_rgb[1:]
    try:
        channels = [int(value[index:index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return hex_rgb
    scaled = [max(0, min(255, int(channel * factor))) for channel in channels]
    return "#" + "".join(f"{channel:02X}" for channel in scaled)


def themed_file(basename: str, theme: str) -> str:
    """
    Return  basename_<theme>.ext  if that file exists, otherwise basename.ext.
    Lets you ship arrowL_dark.png / arrowL_light.png next to arrowL.png.
    """
    root, ext = os.path.splitext(basename)
    themed = f"{root}_{theme}{ext}"
    return themed if os.path.exists(themed) else basename


def themed_icon(basename: str, theme: str) -> QIcon:
    """QIcon wrapper for themed_file()."""
    return QIcon(themed_file(basename, theme))


def _shade(hex_rgb: str, k: float) -> str:
    """Deprecated wrapper; use ``ui.theme.shade_color``."""
    warnings.warn(
        "ui.theme._shade() is deprecated; use ui.theme.shade_color() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return shade_color(hex_rgb, k)


def _apply_header(cal: QCalendarWidget, *, accent="#5C8DBC",
                  fg_white="#FFFFFF", sat_sun="#E53935") -> None:
    """
    Style the built-in header strip using the supplied accent.
    Keeps week-numbers hidden and Sunday/Saturday red.
    """
    accent = shade_color(accent, 1.0)
    fg_white = shade_color(fg_white, 1.0)
    sat_sun = shade_color(sat_sun, 1.0)
    view: QTableView | None = cal.findChild(QTableView)
    if view:
        hh: QHeaderView = view.horizontalHeader()
        pal = hh.palette()
        pal.setColor(QPalette.Base,   QColor(accent))
        pal.setColor(QPalette.Window, QColor(accent))
        pal.setColor(QPalette.Text,   QColor(fg_white))
        hh.setPalette(pal)
        hh.setStyleSheet(
            (
                "QHeaderView::section {"
                f"background:{accent};"
                f"color:{fg_white};"
                "font-weight:600;"
                "font-size:16px;"
                "font-family:Roboto;"
                "border:none;"
                "}"
            )
        )
        for i in range(hh.count()):
            hh.setSectionResizeMode(i, QHeaderView.Stretch)
        vh = view.verticalHeader()
        if vh and vh.isVisible():
            vh.setVisible(False)

    fmt = cal.weekdayTextFormat(Qt.Saturday)
    fmt.setForeground(QColor(sat_sun))
    cal.setWeekdayTextFormat(Qt.Saturday, fmt)
    cal.setWeekdayTextFormat(Qt.Sunday, fmt)
    cal.setGridVisible(True)


def apply_theme_to_calendar(cal: QCalendarWidget, theme: str, accent: str) -> None:
    """
    Give a vanilla QCalendarWidget a dark / light / pink look and apply the
    accent colour for selections.  Uses the generic _apply_header() helper
    that we kept theme-agnostic.
    """
    if theme == "dark":
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#252A32;  color:#E8EAF0;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#252A32;  color:#E8EAF0;
                gridline-color:#3A404B;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #3A404B;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#3A404B;
            }}
        """)
    elif theme == "light":
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#FFFFFF;  color:#2C2A27;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#FFFFFF;  color:#2C2A27;
                gridline-color:#E1DDD6;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #E1DDD6;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#F5F3F0;
            }}
        """)
    else:      # pink
        cal.setStyleSheet(f"""
            QCalendarWidget {{
                background:#F7D7DF;  color:#4A4A4A;
            }}
            QCalendarWidget QAbstractItemView {{
                background:#F7D7DF;  color:#4A4A4A;
                gridline-color:#E9A9B8;
                selection-background-color:{accent};
                selection-color:#FFFFFF;
            }}
            QCalendarWidget QAbstractItemView::item {{
                border:1px solid #E9A9B8;  padding:4px;
            }}
            QCalendarWidget QAbstractItemView::item:hover {{
                background:#F6C3CE;
            }}
        """)

    # header strip (days of week) + red Sat/Sun text
    _apply_header(cal, accent=accent, fg_white="#FFFFFF", sat_sun="#E53935")


def _apply_pink_header(cal: QCalendarWidget, *, accent="#FF4F79",
                       fg_white="#FFFFFF", sat_sun="#E53935") -> None:
    """
    Style the built-in QCalendarWidget: pink/blue header, no week numbers.
    Safe to call multiple times.
    """
    accent = shade_color(accent, 1.0)
    fg_white = shade_color(fg_white, 1.0)
    sat_sun = shade_color(sat_sun, 1.0)
    view: QTableView | None = cal.findChild(QTableView)
    if view:
        # horizontal header (days of week)
        hh: QHeaderView = view.horizontalHeader()
        pal = hh.palette()
        pal.setColor(QPalette.Base,   QColor(accent))
        pal.setColor(QPalette.Window, QColor(accent))
        pal.setColor(QPalette.Text,   QColor(fg_white))
        hh.setPalette(pal)
        hh.setStyleSheet(
            (
                "QHeaderView::section {"
                f"background:{accent};"
                f"color:{fg_white};"
                "font-weight:600;"
                "font-size:16px;"
                "font-family:Roboto;"
                "border:none;"
                "}"
            )
        )
        for i in range(hh.count()):
            hh.setSectionResizeMode(i, QHeaderView.Stretch)

        # *hide* week numbers
        vh = view.verticalHeader()
        if vh and vh.isVisible():
            vh.setVisible(False)

    # Sunday / Saturday text red
    fmt = cal.weekdayTextFormat(Qt.Saturday)
    fmt.setForeground(QColor(sat_sun))
    cal.setWeekdayTextFormat(Qt.Saturday, fmt)
    cal.setWeekdayTextFormat(Qt.Sunday, fmt)

    # make sure grid is on
    cal.setGridVisible(True)


def _fix_selection_contrast(widget: QWidget, accent: str) -> None:
    accent = shade_color(accent, 1.0)
    pal = widget.palette()
    pal.setColor(QPalette.Highlight, QColor(accent))
    pal.setColor(QPalette.HighlightedText, Qt.white)
    widget.setPalette(pal)

GRID_COLOR = QColor(CAL_BORDER)
_GRID = QColor(CAL_BORDER)


def __getattr__(name: str):
    """Retain the former ``ui.theme.UiStyle`` lazy compatibility path."""
    if name == "UiStyle":
        from .style import UiStyle

        return UiStyle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CAL_BORDER",
    "shade_color",
    "themed_file",
    "themed_icon",
    "_apply_header",
    "_apply_pink_header",
    "apply_theme_to_calendar",
    "UiStyle",
]
