"""Calendar-based date picker widgets used by the GUI."""

from __future__ import annotations

from PySide6.QtCore import QDate, QSize, Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import apply_theme_to_calendar, themed_icon


class MultiDatePickerGrid(QCalendarWidget):
    """Internal grid used by ``MultiDatePicker``.

    * Accent-colored blocks mark every ISO date in ``self.selected``.
    * No visible "today"/keyboard-focus/Qt-selection rectangle.
    * Tapping toggles membership in ``self.selected``.
    """

    ACCENT = "#5C8DBC"

    def __init__(self, selected=None, *, accent=None, theme="dark", parent=None):
        super().__init__(parent)
        if accent:
            self.ACCENT = accent
        self._theme = theme

        self.setGridVisible(True)
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

    def setAccent(self, col: str):
        if col and col != self.ACCENT:
            self.ACCENT = col
            for iso in list(self.selected):
                qd = QDate.fromString(iso, "yyyy-MM-dd")
                self._highlight(qd)

    def set_theme(self, theme: str, accent: str):
        self._theme = theme
        self.ACCENT = accent
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
        self._btn_prev = self._arrow(nav, "arrowL.png", prev=True)
        self._lbl_month = QLabel(alignment=Qt.AlignCenter, font=self._HEADER_FONT)
        nav.addWidget(self._lbl_month, 1)
        self._btn_next = self._arrow(nav, "arrowR.png", prev=False)
        root.insertLayout(0, nav)

        dow = QHBoxLayout()
        dow.setContentsMargins(0, 0, 0, 0)
        dow.setSpacing(0)
        self._dow_labels = []
        for txt in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
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
        for i, lbl in enumerate(self._dow_labels):
            fg = "#E53935" if i in (0, 6) else "#FFFFFF"
            lbl.setStyleSheet(f"background:{self._ACCENT};color:{fg};")

    @property
    def selected(self) -> set[str]:
        return self.cal.selected

    def setAccent(self, color: str):
        self._ACCENT = color
        self.cal.setAccent(color)
        self._apply_theme()

    def set_theme(self, theme: str, accent: str):
        self._THEME, self._ACCENT = theme, accent
        self.cal.set_theme(theme, accent)
        self._apply_theme()
        if hasattr(self, "_btn_prev") and hasattr(self, "_btn_next"):
            self._btn_prev.setIcon(themed_icon("arrowL.png", self._THEME))
            self._btn_next.setIcon(themed_icon("arrowR.png", self._THEME))

    def _arrow(self, layout: QHBoxLayout, png: str, *, prev: bool):
        btn = QPushButton(flat=True, cursor=Qt.PointingHandCursor)
        btn.setFixedSize(56, 56)
        btn.setIcon(themed_icon(png, self._THEME))
        btn.setIconSize(QSize(44, 44))
        btn.setStyleSheet("border:none;background:transparent;")
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
    """One-month calendar with external navigation bar.  Uses themed arrows."""

    FONT_HDR = QFont("Roboto", 20, QFont.Bold)
    FONT_DOW = QFont("Roboto", 15, QFont.Bold)
    FONT_GRID = QFont("Roboto", 15)

    def __init__(self, *, accent="#5C8DBC", parent=None, initial=None, theme="dark"):
        super().__init__(parent)
        self.ACCENT = accent
        self._theme = theme
        self._current = initial or QDate.currentDate()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.cal = QCalendarWidget()
        self.cal.setGridVisible(True)
        self.cal.setSelectionMode(QCalendarWidget.SingleSelection)
        self.cal.setNavigationBarVisible(False)
        self.cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.cal.setHorizontalHeaderFormat(QCalendarWidget.NoHorizontalHeader)
        self.cal.setFont(self.FONT_GRID)
        self.cal.setSelectedDate(self._current)

        nav = QHBoxLayout()
        nav.setSpacing(0)
        self.prev_btn = self._nav_btn(nav, "arrowL.png", prev=True)
        self.lbl_month = QLabel(alignment=Qt.AlignCenter, font=self.FONT_HDR)
        nav.addWidget(self.lbl_month, 1)
        self.next_btn = self._nav_btn(nav, "arrowR.png", prev=False)

        dow = QHBoxLayout()
        dow.setSpacing(0)
        self.dow_labels = []
        for d in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
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
        self.cal.clicked.connect(self._on_click)
        self.cal.currentPageChanged.connect(self._update_month)

    def _apply_theme(self):
        apply_theme_to_calendar(self.cal, self._theme, self.ACCENT)
        month_clr = {"dark": "#E8EAF0", "light": "#2C2A27"}.get(self._theme, self.ACCENT)
        self.lbl_month.setStyleSheet(f"color:{month_clr};")
        for i, lbl in enumerate(self.dow_labels):
            fg = "#E53935" if i in (0, 6) else "#FFFFFF"
            lbl.setStyleSheet(f"background:{self.ACCENT};color:{fg};")

    def set_theme(self, theme: str, accent: str):
        self._theme, self.ACCENT = theme, accent
        self._apply_theme()
        self._highlight(self._current)
        if hasattr(self, "prev_btn") and hasattr(self, "next_btn"):
            self.prev_btn.setIcon(themed_icon("arrowL.png", self._theme))
            self.next_btn.setIcon(themed_icon("arrowR.png", self._theme))

    def _nav_btn(self, layout: QHBoxLayout, png: str, *, prev: bool):
        b = QPushButton(flat=True, cursor=Qt.PointingHandCursor)
        b.setFixedSize(56, 56)
        b.setIcon(themed_icon(png, self._theme))
        b.setIconSize(QSize(44, 44))
        b.setStyleSheet("border:none;background:transparent;")
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

    def _on_click(self, qd):
        if qd != self._current:
            self._clear(self._current)
            self._current = qd
            self._highlight(qd)

    def iso(self) -> str:
        return self._current.toString("yyyy-MM-dd")

    def qdate(self) -> QDate:
        return QDate(self._current)

    def sizeHint(self):
        s = self.cal.sizeHint()
        return QSize(max(440, s.width()), s.height() + 70)


__all__ = ["MultiDatePickerGrid", "MultiDatePicker", "SingleDatePicker"]
