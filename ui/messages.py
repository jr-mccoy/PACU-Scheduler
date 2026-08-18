"""Non-blocking information, warning, error, and confirmation dialogs."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .dialogs.tool_dialog import ToolDialog


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


def _message_dialog(parent, title: str, message: str, *,
                    icon: QStyle.StandardPixmap | None = None,
                    accent: str | None = None,
                    text_color: str | None = None) -> ToolDialog:
    host = _resolve_dialog_parent(parent)
    dlg = ToolDialog(host, title)
    dlg.setFixedSize(340, 220)
    if accent:
        dlg.refresh_accent(accent)

    layout = QVBoxLayout()
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    if icon is not None:
        icon_lbl = QLabel(alignment=Qt.AlignCenter)
        pix = _standard_icon(icon)
        if pix is not None:
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedHeight(72)
        layout.addWidget(icon_lbl)

    lbl = QLabel(message, alignment=Qt.AlignCenter, wordWrap=True)
    lbl.setFont(QFont("Roboto", 16))
    if text_color:
        lbl.setStyleSheet(f"color:{text_color};")
    layout.addWidget(lbl)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)

    dlg.setLayout(layout)
    dlg.open()
    return dlg


def show_info(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxInformation)


def show_warning(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxWarning,
                           accent="#F5A623", text_color="#8A6D3B")


def show_error(parent, title, message):
    return _message_dialog(parent, title, message,
                           icon=QStyle.SP_MessageBoxCritical,
                           accent="#E53935", text_color="#B71C1C")


def confirm(invoker: QWidget, title: str, message: str,
            yes_cb: Callable[[], None] | None = None,
            cancel_cb: Callable[[], None] | None = None,
            *, yes_text: str = "Yes", cancel_text: str = "Cancel") -> ToolDialog:
    host = _resolve_dialog_parent(invoker)
    dlg = ToolDialog(host, title)
    dlg.setFixedSize(360, 220)

    layout = QVBoxLayout()
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    icon_lbl = QLabel(alignment=Qt.AlignCenter)
    pix = _standard_icon(QStyle.SP_MessageBoxQuestion)
    if pix is not None:
        icon_lbl.setPixmap(pix)
        icon_lbl.setFixedHeight(72)
    layout.addWidget(icon_lbl)

    lbl = QLabel(message, alignment=Qt.AlignCenter, wordWrap=True)
    lbl.setFont(QFont("Roboto", 16))
    layout.addWidget(lbl)

    buttons = QDialogButtonBox()
    yes_btn = buttons.addButton(yes_text, QDialogButtonBox.AcceptRole)
    cancel_btn = buttons.addButton(cancel_text, QDialogButtonBox.RejectRole)
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
    dlg.open()
    return dlg

__all__ = ["show_info", "show_warning", "show_error", "confirm"]
