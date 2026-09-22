"""Small reusable widgets and delegates shared by dialogs and screens."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..style import UiStyle
from ..theme import arrow_icon

# ─────────────────────────── screen scaffolding ───────────────────────────
# Every screen uses the same title, the same Back button, and the same
# button hierarchy:
#   role="special"      the screen's primary action (at most one per group)
#   role="destructive"  actions that remove data
#   no role             everything else, including Back and Quit
BUTTON_HEIGHT = 48


def set_role(button: QPushButton, role: str | None) -> None:
    """Set a button's style role and re-polish it so the change shows."""
    button.setProperty("role", role)
    style = button.style()
    style.unpolish(button)
    style.polish(button)


def action_button(text: str, role: str | None = None, *, tooltip: str | None = None):
    """A standard-height screen button."""
    btn = QPushButton(text)
    btn.setMinimumHeight(BUTTON_HEIGHT)
    if role:
        btn.setProperty("role", role)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def screen_title(text: str) -> QLabel:
    """The heading shown at the top of every screen."""
    title = QLabel(text)
    title.setFont(UiStyle.TITLE_FONT)
    title.setAlignment(Qt.AlignCenter)
    title.setAccessibleName(text)
    return title


def back_button(screen: QWidget, app, target: str = "main") -> QPushButton:
    """Neutral full-width Back button; Esc on the screen does the same."""
    btn = action_button("Back", tooltip="Return to the main menu (Esc)")
    btn.clicked.connect(lambda: app.switch_frame(target))
    shortcut = QShortcut(QKeySequence(Qt.Key_Escape), screen)
    shortcut.setContext(Qt.WidgetWithChildrenShortcut)
    shortcut.activated.connect(btn.click)
    return btn


def add_shortcut(widget: QWidget, key, callback: Callable[[], None]) -> QShortcut:
    """A shortcut active while focus is inside *widget*."""
    shortcut = QShortcut(QKeySequence(key), widget)
    shortcut.setContext(Qt.WidgetWithChildrenShortcut)
    shortcut.activated.connect(callback)
    return shortcut


def nav_arrow_button(*, prev: bool, tooltip: str, theme: str = "dark") -> QPushButton:
    """Month navigation arrow that renders even without bundled image files.

    Uses ``arrowL.png``/``arrowR.png`` when present, otherwise a large text
    chevron in the theme's text colour (platform arrow icons are often
    dark-on-dark), and zeroes the global button padding that otherwise
    clips small buttons.
    """
    btn = QPushButton(flat=True, cursor=Qt.PointingHandCursor)
    btn.setFixedSize(44, 44)
    btn.setStyleSheet("QPushButton { padding:0; border:none; background:transparent; }")
    btn.setToolTip(tooltip)
    btn.setAccessibleName(tooltip)
    refresh_nav_arrow(btn, prev=prev, theme=theme)
    return btn


def refresh_nav_arrow(btn: QPushButton, *, prev: bool, theme: str) -> None:
    """(Re)apply the arrow artwork for *theme* to a ``nav_arrow_button``."""
    icon = arrow_icon(prev, theme)
    if icon is None:
        btn.setIcon(QIcon())
        btn.setText("‹" if prev else "›")
        font = btn.font()
        font.setPointSize(26)
        font.setBold(True)
        btn.setFont(font)
    else:
        btn.setText("")
        btn.setIcon(icon)
        btn.setIconSize(QSize(28, 28))


class _EmptyStateFilter(QObject):
    """Paints a centred hint over an item view's viewport while it is empty."""

    def __init__(self, view: QAbstractItemView, text: str):
        super().__init__(view)
        self._view = view
        self.text = text
        view.viewport().installEventFilter(self)

    def _is_empty(self) -> bool:
        view = self._view
        if isinstance(view, QListWidget):
            return all(view.item(i).isHidden() for i in range(view.count()))
        if isinstance(view, QTableWidget):
            return view.rowCount() == 0
        model = view.model()
        return model is None or model.rowCount() == 0

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Paint and self.text and self._is_empty():
            # Let the view paint its background first, then draw the hint.
            obj.removeEventFilter(self)
            QApplication.sendEvent(obj, event)
            obj.installEventFilter(self)
            painter = QPainter(obj)
            painter.setPen(self._view.palette().placeholderText().color())
            rect = obj.rect().adjusted(24, 24, -24, -24)
            painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, self.text)
            painter.end()
            return True
        return False


def install_empty_state(view: QAbstractItemView, text: str) -> _EmptyStateFilter:
    """Show *text* in *view* whenever it has no (visible) rows.

    Returns the filter; assign to its ``text`` attribute to change the
    message (e.g. "No matches" while a search is active).
    """
    return _EmptyStateFilter(view, text)


class WrappedCheck(QWidget):
    """
    A checkbox that wraps its text to multiple lines on narrow screens.
    Exposes isChecked()/setChecked() and toggled(bool) like QCheckBox.
    """

    toggled = Signal(bool)

    def __init__(self, text: str, checked: bool = False, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._cb = QCheckBox("")
        self._cb.setChecked(checked)
        self._cb.toggled.connect(self.toggled)

        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # Tap label to toggle checkbox
        self._label.mousePressEvent = lambda e: self._cb.toggle()

        lay.addWidget(self._cb, 0, Qt.AlignTop)
        lay.addWidget(self._label, 1)

    def isChecked(self) -> bool:
        return self._cb.isChecked()

    def setChecked(self, v: bool) -> None:
        self._cb.setChecked(v)

    def set_wrap_width(self, w: int) -> None:
        w = max(100, int(w))
        self._label.setFixedWidth(w)


class ConfirmOverlay(QWidget):
    def __init__(self, parent, title, message, yes_cb=None, cancel_cb=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Fill the parent
        self.setGeometry(parent.rect())
        self.setStyleSheet("background:rgba(0,0,0,0.3);")

        card = QWidget(self)
        card.setStyleSheet("""
            background: white; border-radius: 18px;
            padding: 24px;
        """)
        card.setFixedWidth(360)
        card.setFixedHeight(180)
        card.move((self.width() - card.width()) // 2, (self.height() - card.height()) // 2)

        v = QVBoxLayout(card)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(16)
        lbl_title = QLabel(title)
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("font-size:18pt;font-weight:bold;")
        v.addWidget(lbl_title)

        lbl_msg = QLabel(message)
        lbl_msg.setAlignment(Qt.AlignCenter)
        lbl_msg.setWordWrap(True)
        v.addWidget(lbl_msg)

        h = QHBoxLayout()
        btn_yes = QPushButton("Yes")
        btn_no = QPushButton("Cancel")
        btn_yes.setMinimumWidth(90)
        btn_no.setMinimumWidth(90)
        h.addWidget(btn_yes)
        h.addWidget(btn_no)
        v.addLayout(h)

        btn_yes.clicked.connect(lambda: self._respond(True, yes_cb, cancel_cb))
        btn_no.clicked.connect(lambda: self._respond(False, yes_cb, cancel_cb))

    def _respond(self, accepted, yes_cb, cancel_cb):
        self.close()
        if accepted and yes_cb:
            yes_cb()
        elif not accepted and cancel_cb:
            cancel_cb()


class WrapDelegate(QStyledItemDelegate):
    """Enables word-wrap for QListWidget/QTableView items."""

    def initStyleOption(self, option: QStyleOptionViewItem, index):
        super().initStyleOption(option, index)
        option.features |= QStyleOptionViewItem.WrapText


__all__ = [
    "BUTTON_HEIGHT",
    "WrappedCheck",
    "ConfirmOverlay",
    "WrapDelegate",
    "action_button",
    "add_shortcut",
    "back_button",
    "install_empty_state",
    "nav_arrow_button",
    "refresh_nav_arrow",
    "screen_title",
    "set_role",
]
