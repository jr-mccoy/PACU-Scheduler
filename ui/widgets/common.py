"""Small reusable widgets and delegates shared by dialogs and screens."""

from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)


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
        card.move(
            (self.width() - card.width()) // 2,
            (self.height() - card.height()) // 2
        )

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

__all__ = ["WrappedCheck", "ConfirmOverlay", "WrapDelegate"]
