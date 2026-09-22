"""Non-blocking information, warning, error, and confirmation dialogs."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .dialogs.tool_dialog import ToolDialog
from .style import UiStyle

_MESSAGE_WIDTH = 440
_MESSAGE_MIN_HEIGHT = 200


def _resolve_dialog_parent(widget: QWidget | None) -> QWidget | None:
    if widget is None or not isinstance(widget, QWidget):
        return widget

    parent = widget
    while isinstance(parent, QWidget) and not hasattr(parent, "settings"):
        next_parent = parent.parentWidget()
        if next_parent is None:
            break
        parent = next_parent

    if isinstance(parent, QWidget) and hasattr(parent, "settings"):
        return parent

    window = widget.window() if isinstance(widget, QWidget) else None
    if isinstance(window, QWidget) and hasattr(window, "settings"):
        return window

    return widget


def _standard_icon(icon: QStyle.StandardPixmap, size: int = 64) -> QPixmap | None:
    style = QApplication.style()
    if not style:
        return None
    pix = style.standardIcon(icon).pixmap(QSize(size, size))
    return pix if not pix.isNull() else None


def _fit_to_content(dlg: ToolDialog, width: int = _MESSAGE_WIDTH) -> None:
    """Size a message dialog to its wrapped text instead of a fixed box.

    The body lives in a scroll area, so the dialog's own size hint says
    nothing about the content; ask the body layout how tall it needs to be
    at the chosen width and cap the result to most of the screen.
    """
    layout = dlg._body.layout()
    if layout is None:
        return
    card_margins = dlg._main.contentsMargins()
    inner_width = width - card_margins.left() - card_margins.right()
    if layout.hasHeightForWidth():
        height = layout.heightForWidth(inner_width)
    else:
        height = layout.sizeHint().height()
    height += card_margins.top() + card_margins.bottom() + 8

    max_height = 640
    screen = QApplication.primaryScreen()
    if screen is not None:
        max_height = int(screen.availableGeometry().height() * 0.8)
    dlg.setMinimumSize(320, _MESSAGE_MIN_HEIGHT)
    dlg.resize(width, max(_MESSAGE_MIN_HEIGHT, min(height, max_height)))


def _message_label(message: str, text_color: str | None = None) -> QLabel:
    lbl = QLabel(message, alignment=Qt.AlignCenter, wordWrap=True)
    lbl.setFont(QFont("Roboto", 14))
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if text_color:
        lbl.setStyleSheet(f"color:{text_color};")
    return lbl


def _message_dialog(
    parent,
    title: str,
    message: str,
    *,
    icon: QStyle.StandardPixmap | None = None,
    accent: str | None = None,
    text_color: str | None = None,
    details: str | None = None,
) -> ToolDialog:
    host = _resolve_dialog_parent(parent)
    dlg = ToolDialog(host, title)
    if accent:
        dlg.refresh_accent(accent)

    layout = QVBoxLayout()
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(14)

    if icon is not None:
        icon_lbl = QLabel(alignment=Qt.AlignCenter)
        pix = _standard_icon(icon, 48)
        if pix is not None:
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedHeight(56)
        layout.addWidget(icon_lbl)

    layout.addWidget(_message_label(message, text_color))

    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.accepted.connect(dlg.accept)

    if details:
        # Technical detail (e.g. a traceback) stays out of the way until asked
        # for; it is still selectable so it can be copied into a bug report.
        text = QPlainTextEdit(details)
        text.setReadOnly(True)
        text.setMinimumHeight(180)
        text.setVisible(False)
        layout.addWidget(text)
        toggle = QPushButton("Show details")
        toggle.setAutoDefault(False)
        buttons.addButton(toggle, QDialogButtonBox.ActionRole)

        def _toggle_details():
            showing = not text.isVisible()
            text.setVisible(showing)
            toggle.setText("Hide details" if showing else "Show details")
            _fit_to_content(dlg, 640 if showing else _MESSAGE_WIDTH)

        toggle.clicked.connect(_toggle_details)

    layout.addWidget(buttons)

    dlg.setLayout(layout)
    _fit_to_content(dlg)
    dlg.open()
    return dlg


def show_info(parent, title, message):
    return _message_dialog(parent, title, message, icon=QStyle.SP_MessageBoxInformation)


def show_warning(parent, title, message):
    colours = UiStyle.palette(UiStyle.current_theme())
    return _message_dialog(
        parent,
        title,
        message,
        icon=QStyle.SP_MessageBoxWarning,
        accent=colours["warning"],
        text_color=colours["warning_text"],
    )


def show_error(parent, title, message, details: str | None = None):
    """Show an error.  ``details`` (e.g. a traceback) is hidden behind a toggle."""
    colours = UiStyle.palette(UiStyle.current_theme())
    return _message_dialog(
        parent,
        title,
        message,
        icon=QStyle.SP_MessageBoxCritical,
        accent=colours["error"],
        text_color=colours["error_text"],
        details=details,
    )


def confirm(
    invoker: QWidget,
    title: str,
    message: str,
    yes_cb: Callable[[], None] | None = None,
    cancel_cb: Callable[[], None] | None = None,
    *,
    yes_text: str = "Yes",
    cancel_text: str = "Cancel",
    destructive: bool = False,
) -> ToolDialog:
    """Ask a yes/cancel question.

    Name the action in ``yes_text`` ("Remove", "Apply") rather than "Yes".
    With ``destructive=True`` the confirm button is styled as dangerous and
    Cancel, not the destructive action, is what Enter presses.
    """
    host = _resolve_dialog_parent(invoker)
    dlg = ToolDialog(host, title)

    layout = QVBoxLayout()
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(14)

    icon_lbl = QLabel(alignment=Qt.AlignCenter)
    pix = _standard_icon(
        QStyle.SP_MessageBoxWarning if destructive else QStyle.SP_MessageBoxQuestion, 48
    )
    if pix is not None:
        icon_lbl.setPixmap(pix)
        icon_lbl.setFixedHeight(56)
    layout.addWidget(icon_lbl)

    layout.addWidget(_message_label(message))

    buttons = QDialogButtonBox()
    yes_btn = buttons.addButton(yes_text, QDialogButtonBox.AcceptRole)
    cancel_btn = buttons.addButton(cancel_text, QDialogButtonBox.RejectRole)
    if destructive:
        yes_btn.setProperty("role", "destructive")
        yes_btn.setAutoDefault(False)
        cancel_btn.setDefault(True)
    else:
        yes_btn.setProperty("role", "special")
        yes_btn.setDefault(True)
        cancel_btn.setAutoDefault(False)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if yes_cb:
        dlg.accepted.connect(yes_cb)
    if cancel_cb:
        dlg.rejected.connect(cancel_cb)

    dlg.setLayout(layout)
    _fit_to_content(dlg)
    dlg.open()
    return dlg


__all__ = ["show_info", "show_warning", "show_error", "confirm"]
