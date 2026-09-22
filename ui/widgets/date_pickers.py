"""Calendar-based date picker widgets used by the GUI."""

from __future__ import annotations

from PySide6.QtCore import QDate, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import apply_theme_to_calendar, shade_color, weekend_color
from .common import nav_arrow_button, refresh_nav_arrow

# The day-of-week bar is drawn by these widgets rather than by Qt, so the
# calendar grid must be pinned to the same first day or every column label
# is off by one on locales whose week starts on Monday.
DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
FIRST_DAY = Qt.Sunday


def _style_dow_labels(labels: list[QLabel], accent: str) -> None:
    """Accent bar with white text; weekends get a darker shade, not red-on-blue."""
    weekend_bg = shade_color(accent, 0.75)
    for i, lbl in enumerate(labels):
        bg = weekend_bg if i in (0, 6) else accent
        lbl.setStyleSheet(f"background:{bg};color:#FFFFFF;padding:2px 0;")


class MultiDatePickerGrid(QCalendarWidget):
    """Internal grid used by ``MultiDatePicker``.

    * Accent-colored blocks mark every ISO date in ``self.selected``.
    * No visible "today"/keyboard-focus/Qt-selection rectangle.
    * Tapping toggles membership in ``self.selected``.
    """

    ACCENT = "#5C8DBC"

    def __init__(self, selected=None, *, accent=None, theme="dark", parent=None, grid=True):
        super().__init__(parent)
        if accent:
            self.ACCENT = accent
        self._theme = theme

        self.setFirstDayOfWeek(FIRST_DAY)
        self.setGridVisible(grid)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.NoHorizontalHeader)
        self.setNavigationBarVisible(False)
        self.setSelectionMode(QCalendarWidget.SingleSelection)

        self._apply_theme()

        self.setDateTextFormat(QDate.currentDate(), QTextCharFormat())

        self.selected = set(selected or [])
        for iso in list(self.selected):
            qd = QDate.fromString(iso, "yyyy-MM-dd")
            if qd.isValid():
                self._highlight(qd)

        self.clicked.connect(self._toggle)

    def _apply_theme(self):
        if self._theme == "dark":
            self.setStyleSheet(
                """
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #E8EAF0;
                    background: #252A32;
                    color: #E8EAF0;
                }
                QCalendarWidget QAbstractItemView {
                    background: #252A32;
                    color: #E8EAF0;
                    gridline-color: #3A404B;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #3A404B;
                }
                """
            )
        elif self._theme == "light":
            self.setStyleSheet(
                """
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #2C2A27;
                    background: #FFFFFF;
                    color: #2C2A27;
                }
                QCalendarWidget QAbstractItemView {
                    background: #FFFFFF;
                    color: #2C2A27;
                    gridline-color: #E1DDD6;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #E1DDD6;
                }
                """
            )
        else:
            self.setStyleSheet(
                """
                QCalendarWidget QWidget#qt_calendar_calendarview {
                    selection-background-color: transparent;
                    selection-color: #4A4A4A;
                    background: #F7D7DF;
                    color: #4A4A4A;
                }
                QCalendarWidget QAbstractItemView {
                    background: #F7D7DF;
                    color: #4A4A4A;
                    gridline-color: #E9A9B8;
                }
                QCalendarWidget QAbstractItemView::item {
                    border: 1px solid #E9A9B8;
                }
                """
            )
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(weekend_color(self._theme)))
        self.setWeekdayTextFormat(Qt.Saturday, fmt)
        self.setWeekdayTextFormat(Qt.Sunday, fmt)

    def setAccent(self, col: str):
        if col and col != self.ACCENT:
            self.ACCENT = col
            for iso in list(self.selected):
                qd = QDate.fromString(iso, "yyyy-MM-dd")
                self._highlight(qd)

    def set_theme(self, theme: str, accent: str, *, grid: bool | None = None):
        self._theme = theme
        self.ACCENT = accent
        if grid is not None:
            self.setGridVisible(grid)
        self._apply_theme()
        for iso in list(self.selected):
            qd = QDate.fromString(iso, "yyyy-MM-dd")
            if qd.isValid():
                self._highlight(qd)

    def _toggle(self, qd: QDate):
        iso = qd.toString("yyyy-MM-dd")
        if iso in self.selected:
            self.selected.remove(iso)
            self.setDateTextFormat(qd, QTextCharFormat())
        else:
            self.selected.add(iso)
            self._highlight(qd)

    def _highlight(self, qd: QDate):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self.ACCENT))
        fmt.setForeground(Qt.white)
        fmt.setFontWeight(QFont.Bold)
        self.setDateTextFormat(qd, fmt)

    def paintCell(self, painter, rect, qdate):
        super().paintCell(painter, rect, qdate)

        iso = qdate.toString("yyyy-MM-dd")

        if iso in self.selected:
            painter.save()
            painter.fillRect(rect, QColor(self.ACCENT))
            painter.setPen(Qt.white)
            f = painter.font()
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(rect, Qt.AlignCenter, str(qdate.day()))
            painter.restore()

        elif qdate == self.selectedDate():
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.palette().base())
            painter.drawRect(rect)
            painter.setPen(self.palette().text().color())
            painter.drawText(rect, Qt.AlignCenter, str(qdate.day()))
            painter.restore()

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(max(440, s.width()), s.height())


class MultiDatePicker(QWidget):
    """Month-header + day-of-week bar + ``MultiDatePickerGrid``."""

    _HEADER_FONT = QFont("Roboto", 20, QFont.Bold)
    _DOW_FONT = QFont("Roboto", 15, QFont.Bold)

    def __init__(self, selected=None, *, accent="#5C8DBC", theme="dark", parent=None):
        super().__init__(parent)
        self._ACCENT = accent
        self._THEME = theme

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.cal = MultiDatePickerGrid(selected, accent=accent, theme=theme)
        root.addWidget(self.cal, 1)

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(0)
        self._btn_prev = self._arrow(nav, prev=True)
        self._lbl_month = QLabel(alignment=Qt.AlignCenter, font=self._HEADER_FONT)
        nav.addWidget(self._lbl_month, 1)
        self._btn_next = self._arrow(nav, prev=False)
        root.insertLayout(0, nav)

        dow = QHBoxLayout()
        dow.setContentsMargins(0, 0, 0, 0)
        dow.setSpacing(0)
        self._dow_labels = []
        for txt in DAY_NAMES:
            lbl = QLabel(txt, alignment=Qt.AlignCenter, font=self._DOW_FONT)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            dow.addWidget(lbl)
            self._dow_labels.append(lbl)
        root.insertLayout(1, dow)

        self._apply_theme()
        self._refresh_month()
        self.cal.currentPageChanged.connect(self._refresh_month)

    def _apply_theme(self):
        month_clr = {"dark": "#E8EAF0", "light": "#2C2A27"}.get(self._THEME, self._ACCENT)
        self._lbl_month.setStyleSheet(f"color:{month_clr};")
        _style_dow_labels(self._dow_labels, self._ACCENT)

    @property
    def selected(self) -> set[str]:
        return self.cal.selected

    def setAccent(self, color: str):
        self._ACCENT = color
        self.cal.setAccent(color)
        self._apply_theme()

    def set_theme(self, theme: str, accent: str, *, grid: bool | None = None):
        self._THEME, self._ACCENT = theme, accent
        self.cal.set_theme(theme, accent, grid=grid)
        self._apply_theme()
        refresh_nav_arrow(self._btn_prev, prev=True, theme=theme)
        refresh_nav_arrow(self._btn_next, prev=False, theme=theme)

    def _arrow(self, layout: QHBoxLayout, *, prev: bool):
        btn = nav_arrow_button(
            prev=prev, tooltip="Previous month" if prev else "Next month", theme=self._THEME
        )
        btn.clicked.connect(self.cal.showPreviousMonth if prev else self.cal.showNextMonth)
        btn.clicked.connect(self._refresh_month)
        layout.addWidget(btn)
        return btn

    def _refresh_month(self, *_):
        y, m = self.cal.yearShown(), self.cal.monthShown()
        self._lbl_month.setText(QDate(y, m, 1).toString("MMMM  yyyy"))

    def sizeHint(self):
        return self.cal.sizeHint()


class SingleDatePicker(QWidget):
    """One-month calendar with external navigation bar.

    ``dateChanged`` fires whenever the picked date changes, by mouse or by
    keyboard.
    """

    dateChanged = Signal(QDate)

    FONT_HDR = QFont("Roboto", 20, QFont.Bold)
    FONT_DOW = QFont("Roboto", 15, QFont.Bold)
    FONT_GRID = QFont("Roboto", 15)

    def __init__(self, *, accent="#5C8DBC", parent=None, initial=None, theme="dark", grid=True):
        super().__init__(parent)
        self.ACCENT = accent
        self._theme = theme
        self._grid = grid
        self._current = initial or QDate.currentDate()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.cal = QCalendarWidget()
        self.cal.setFirstDayOfWeek(FIRST_DAY)
        self.cal.setGridVisible(grid)
        self.cal.setSelectionMode(QCalendarWidget.SingleSelection)
        self.cal.setNavigationBarVisible(False)
        self.cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.cal.setHorizontalHeaderFormat(QCalendarWidget.NoHorizontalHeader)
        self.cal.setFont(self.FONT_GRID)
        self.cal.setSelectedDate(self._current)

        nav = QHBoxLayout()
        nav.setSpacing(0)
        self.prev_btn = self._nav_btn(nav, prev=True)
        self.lbl_month = QLabel(alignment=Qt.AlignCenter, font=self.FONT_HDR)
        nav.addWidget(self.lbl_month, 1)
        self.next_btn = self._nav_btn(nav, prev=False)

        dow = QHBoxLayout()
        dow.setSpacing(0)
        self.dow_labels = []
        for d in DAY_NAMES:
            lbl = QLabel(d, alignment=Qt.AlignCenter, font=self.FONT_DOW)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            dow.addWidget(lbl)
            self.dow_labels.append(lbl)

        root.addLayout(nav)
        root.addLayout(dow)
        root.addWidget(self.cal, 1)

        self._apply_theme()
        self._highlight(self._current)
        self._update_month()
        # selectionChanged covers keyboard navigation as well as clicks.
        self.cal.selectionChanged.connect(self._on_selection_changed)
        self.cal.currentPageChanged.connect(self._update_month)

    def _apply_theme(self):
        apply_theme_to_calendar(self.cal, self._theme, self.ACCENT, grid=self._grid)
        month_clr = {"dark": "#E8EAF0", "light": "#2C2A27"}.get(self._theme, self.ACCENT)
        self.lbl_month.setStyleSheet(f"color:{month_clr};")
        _style_dow_labels(self.dow_labels, self.ACCENT)

    def set_theme(self, theme: str, accent: str, *, grid: bool | None = None):
        self._theme, self.ACCENT = theme, accent
        if grid is not None:
            self._grid = grid
        self._apply_theme()
        self._highlight(self._current)
        refresh_nav_arrow(self.prev_btn, prev=True, theme=theme)
        refresh_nav_arrow(self.next_btn, prev=False, theme=theme)

    def _nav_btn(self, layout: QHBoxLayout, *, prev: bool):
        b = nav_arrow_button(
            prev=prev, tooltip="Previous month" if prev else "Next month", theme=self._theme
        )
        b.clicked.connect(self.cal.showPreviousMonth if prev else self.cal.showNextMonth)
        b.clicked.connect(self._update_month)
        layout.addWidget(b)
        return b

    def _update_month(self, *_):
        y, m = self.cal.yearShown(), self.cal.monthShown()
        self.lbl_month.setText(QDate(y, m, 1).toString("MMMM  yyyy"))

    def _highlight(self, qd: QDate):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self.ACCENT))
        fmt.setForeground(Qt.white)
        fmt.setFontWeight(QFont.Bold)
        self.cal.setDateTextFormat(qd, fmt)

    def _clear(self, qd):
        self.cal.setDateTextFormat(qd, QTextCharFormat())

    def _on_selection_changed(self):
        self._select(self.cal.selectedDate())

    def _select(self, qd: QDate):
        if qd.isValid() and qd != self._current:
            self._clear(self._current)
            self._current = QDate(qd)
            self._highlight(qd)
            self.dateChanged.emit(QDate(qd))

    def set_date(self, qd: QDate) -> None:
        """Pick *qd* programmatically and show its month."""
        if not qd.isValid():
            return
        self.cal.setSelectedDate(qd)  # emits selectionChanged -> _select
        self._select(qd)
        self.cal.setCurrentPage(qd.year(), qd.month())

    def iso(self) -> str:
        return self._current.toString("yyyy-MM-dd")

    def qdate(self) -> QDate:
        return QDate(self._current)

    def sizeHint(self):
        s = self.cal.sizeHint()
        return QSize(max(440, s.width()), s.height() + 70)


__all__ = ["MultiDatePickerGrid", "MultiDatePicker", "SingleDatePicker"]
