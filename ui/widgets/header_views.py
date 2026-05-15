"""Header view subclasses used by the GUI's table widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QStyle, QStyleOptionHeader


class PinkHeaderView(QHeaderView):
    """Legacy header view kept so runtime imports don't fail.

    It now just calls the base implementation — all header styling is
    done by ``apply_theme_*`` helpers.
    """

    def __init__(self, orientation, parent=None, *_, **__):
        super().__init__(orientation, parent)

    def paintSection(self, painter, rect, logicalIndex):
        super().paintSection(painter, rect, logicalIndex)


class MultiLineHeaderView(QHeaderView):
    """Header view that word-wraps section text and uses a custom height."""

    def __init__(self, orientation, parent=None, height=56):
        super().__init__(orientation, parent)
        self._custom_height = height

    def paintSection(self, painter, rect, logicalIndex):
        if not rect.isValid():
            return
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = rect
        opt.section = logicalIndex
        self.style().drawControl(QStyle.CE_Header, opt, painter, self)
        painter.save()
        painter.setFont(self.font())
        painter.setPen(Qt.black)
        txt = self.model().headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        if txt is not None:
            painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, str(txt))
        painter.restore()

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(self._custom_height)
        return sh


class TallHeaderView(QHeaderView):
    """Header view with an enlarged fixed height."""

    def __init__(self, orientation, parent=None, height=64):
        super().__init__(orientation, parent)
        self._custom_height = height

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(self._custom_height)
        return sh


__all__ = ["PinkHeaderView", "MultiLineHeaderView", "TallHeaderView"]
