"""Base ``ToolDialog`` implementation used by every GUI dialog.

Non-blocking dialog that shows a darkened scrim and a rounded card.
The card border colour is the current accent colour taken from the
parent App (falls back to the default blue accent when no settings are
available).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..theme import shade_color


def _is_android_platform() -> bool:
    """Return True when running inside any Android/Python-for-Android build."""
    import os
    import sys

    platform_plugin = os.environ.get("QT_QPA_PLATFORM", "").lower()
    return (
        sys.platform == "android"
        or "ANDROID_ROOT" in os.environ
        or "ANDROID_DATA" in os.environ
        or "ANDROID_STORAGE" in os.environ
        or "ANDROID_ARGUMENT" in os.environ
        or platform_plugin == "android"
        or hasattr(sys, "getandroidapilevel")
    )


class ToolDialog(QWidget):
    """Non-blocking dialog with a darkened scrim and a rounded card."""

    accepted = Signal()
    rejected = Signal()

    def __init__(self, parent=None, title: str | None = None):
        super().__init__(parent)

        self.is_android = _is_android_platform()
        if not self.is_android:
            self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.CustomizeWindowHint)
            self.setWindowModality(Qt.WindowModal)
        if title:
            self.setWindowTitle(title)

        self._scrim = QWidget(self, objectName="scrim")
        self._scrim.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._scrim.setStyleSheet("QWidget#scrim { background:rgba(0,0,0,0.18); }")

        accent = "#5C8DBC"
        if parent is not None:
            accent = parent.palette().color(QPalette.Highlight).name()

        self.setAttribute(Qt.WA_TranslucentBackground)
        self._card = QWidget(self, objectName="card")
        self._apply_card_style(accent)
        shadow = QGraphicsDropShadowEffect(
            blurRadius=24, xOffset=0, yOffset=4, color=QColor(0, 0, 0, 90)
        )
        self._card.setGraphicsEffect(shadow)

        self._main = QVBoxLayout(self._card)
        self._main.setContentsMargins(24, 24, 24, 24)
        self._main.setSpacing(0)
        self._main_layout = self._main

        self._scroll = QScrollArea(frameShape=QScrollArea.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._scroll.setWidget(self._body)
        self._main.addWidget(self._scroll)

        self._set_default_size()

    def _apply_card_style(self, accent: str) -> None:
        self._card.setStyleSheet(
            f"""
            QWidget#card {{
                border:4px solid {accent};
                border-radius:16px;
                background:palette(window);
            }}
            QWidget#card QLineEdit,
            QWidget#card QAbstractSpinBox,
            QWidget#card QPlainTextEdit,
            QWidget#card QTextEdit,
            QWidget#card QComboBox {{
                background:palette(base);
            }}
            QWidget#card QCheckBox::indicator {{
                width:20px; height:20px;
                background:palette(base);
                border:1px solid #888; border-radius:3px;
            }}
            QWidget#card QCheckBox::indicator:checked {{
                background:{accent}; border:2px solid {accent};
            }}
            """
        )

    def refresh_accent(self, accent: str | None = None) -> None:
        """Re-write the card style with a (new) accent colour."""
        if accent is None:
            accent = QApplication.palette().color(QPalette.Highlight).name()
        self._apply_card_style(accent)

    @staticmethod
    def _fix_selection_contrast(w: QWidget, accent: str) -> None:
        """Force text to stay readable when accenting selection background."""
        accent = shade_color(accent, 1.0)
        pal = w.palette()
        pal.setColor(QPalette.Highlight, QColor(accent))
        pal.setColor(QPalette.HighlightedText, Qt.white)
        w.setPalette(pal)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._scrim.resize(self.size())
        self._card.resize(self.size())

    def setLayout(self, layout):
        """Call AFTER you build the content layout."""
        self._body.setLayout(layout)

    def open(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def accept(self):
        self.close()
        self.accepted.emit()

    def reject(self):
        self.close()
        self.rejected.emit()

    def exec(self, *_, **__):
        raise RuntimeError("Use .open() for non-blocking behaviour")

    def _set_default_size(self):
        scr = QApplication.primaryScreen()
        if self.is_android and scr:
            sz = scr.size()
            self.resize(int(sz.width() * 0.9), int(sz.height() * 0.8))
        else:
            self.resize(640, 480)


__all__ = ["ToolDialog"]
