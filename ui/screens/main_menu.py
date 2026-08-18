"""Main menu screen — top-level navigation hub."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..style import UiStyle


class MainMenu(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.settings = parent.settings

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        title = QLabel("Nurse Scheduler")
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        # animated GIF
        self.gif_lbl = QLabel(alignment=Qt.AlignCenter)
        self.gif_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.gif_lbl)
        self.movie = QMovie("GIF.gif")
        self.movie.setCacheMode(QMovie.CacheAll)
        self.movie.setSpeed(100)
        self.gif_lbl.setMovie(self.movie)
        self._aspect = 16 / 9
        self.movie.frameChanged.connect(self._capture_aspect_once)
        self.update_gif(self.settings.get("show_gif"))
        lay.addSpacing(8)

        # buttons
        for text, name in [
            ("Manage Nurses", "manage"),
            ("Pre-scheduled Assignments", "prescheduled"),
            ("Assignment History", "assignment_hist"),
            ("Weekend History", "weekend_history"),
            ("View Unavailable Dates", "view_unavail"),
            ("Generate Schedule", "generate"),
            ("Advanced Weekend Stats", "advanced_stats"),
            ("Settings", "settings"),
            ("Quit", None),
        ]:
            btn = QPushButton(text)
            btn.setMinimumHeight(40)
            if name == "settings":
                btn.setProperty("role", "special")
                btn.clicked.connect(parent.open_settings_dialog)
            elif name:
                btn.clicked.connect(lambda _, n=name: parent.switch_frame(n))
            else:
                btn.setProperty("role", "destructive")
                btn.clicked.connect(parent.close)
            lay.addWidget(btn)

    def _capture_aspect_once(self, _):
        fr = self.movie.frameRect()
        if fr.height() > 0:
            self._aspect = fr.width() / fr.height()
            self.movie.frameChanged.disconnect(self._capture_aspect_once)
            QTimer.singleShot(0, self._rescale_movie)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._rescale_movie()

    def _available_width(self) -> int:
        lay = self.layout()
        if lay is None:
            return self.width()
        m = lay.contentsMargins()
        return self.width() - (m.left() + m.right())

    def _rescale_movie(self):
        if not self.gif_lbl.isVisible() or self._aspect == 0:
            return
        w = self.gif_lbl.width()
        if w == 0:
            m = self.layout().contentsMargins()
            w = self.width() - (m.left() + m.right())
        h = int(w / self._aspect)
        self.gif_lbl.setFixedSize(w, h)
        self.movie.setScaledSize(QSize(w, h))

    def update_gif(self, show: bool):
        self.gif_lbl.setVisible(show)
        if show:
            if self.movie.state() != QMovie.Running:
                self.movie.start()
            QTimer.singleShot(0, self._rescale_movie)
        else:
            self.movie.stop()


__all__ = ["MainMenu"]
