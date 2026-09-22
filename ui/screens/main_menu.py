"""Main menu screen — top-level navigation hub."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QMovie
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_TITLE
from ..style import UiStyle
from ..widgets.common import action_button, add_shortcut

GIF_PATH = "GIF.gif"


class MainMenu(QWidget):
    # (heading, [(label, page name or action, role, tooltip)])
    SECTIONS = [
        (
            "Schedule",
            [
                (
                    "Generate Schedule",
                    "generate",
                    "special",
                    "Pick a date range and build candidate schedules",
                ),
                (
                    "Pre-scheduled Assignments",
                    "prescheduled",
                    None,
                    "Pin Main/Backup nurses to dates before generating",
                ),
            ],
        ),
        (
            "Roster",
            [
                (
                    "Manage Nurses",
                    "manage",
                    None,
                    "Add or remove nurses and set PRN / late-shift status",
                ),
                (
                    "Unavailable Dates",
                    "view_unavail",
                    None,
                    "Review and edit every nurse's time off",
                ),
            ],
        ),
        (
            "History",
            [
                (
                    "Assignment History",
                    "assignment_hist",
                    None,
                    "Past Main/Backup assignments used for fairness",
                ),
                ("Weekend History", "weekend_history", None, "Past FSF/SFS weekend rotations"),
                (
                    "Rotation Violation Stats",
                    "advanced_stats",
                    None,
                    "Per-nurse weekend rotation repeats and streaks",
                ),
            ],
        ),
    ]

    MAX_COLUMN_WIDTH = 480

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.settings = parent.settings

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addStretch(1)

        column_host = QWidget()
        column_host.setMaximumWidth(self.MAX_COLUMN_WIDTH)
        column_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        outer.addWidget(column_host, 4)
        outer.addStretch(1)

        lay = QVBoxLayout(column_host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addStretch(1)

        title = QLabel(APP_TITLE)
        title.setFont(UiStyle.TITLE_FONT)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        subtitle = QLabel("On-call scheduling for the post-anesthesia care unit")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setProperty("role", "muted")
        subtitle.setWordWrap(True)
        lay.addWidget(subtitle)

        # Optional animated GIF.  The file is not shipped with the repository;
        # when it is missing the label stays hidden instead of reserving an
        # empty 16:9 block above the buttons.
        self.gif_lbl = QLabel(alignment=Qt.AlignCenter)
        self.gif_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.gif_lbl)
        self.movie = QMovie(GIF_PATH)
        self.movie.setCacheMode(QMovie.CacheAll)
        self.movie.setSpeed(100)
        self.gif_lbl.setMovie(self.movie)
        self._aspect = 16 / 9
        self.movie.frameChanged.connect(self._capture_aspect_once)
        self.update_gif(self.settings.get("show_gif"))
        lay.addSpacing(8)

        self.buttons: dict[str, QPushButton] = {}
        for heading, entries in self.SECTIONS:
            lay.addSpacing(6)
            lay.addWidget(self._section_label(heading))
            for text, name, role, tip in entries:
                btn = action_button(text, role, tooltip=tip)
                btn.clicked.connect(lambda _, n=name: parent.switch_frame(n))
                lay.addWidget(btn)
                self.buttons[name] = btn

        lay.addSpacing(14)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        settings_btn = action_button("Settings", tooltip="Theme, scheduling rules and diagnostics")
        settings_btn.clicked.connect(parent.open_settings_dialog)
        quit_btn = action_button("Quit", tooltip="Close the application (Ctrl+Q)")
        quit_btn.clicked.connect(parent.close)
        bottom.addWidget(settings_btn)
        bottom.addWidget(quit_btn)
        lay.addLayout(bottom)
        self.buttons["settings"] = settings_btn
        self.buttons["quit"] = quit_btn
        add_shortcut(parent, QKeySequence.Quit, parent.close)
        lay.addStretch(1)

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setProperty("role", "muted")
        font = lbl.font()
        font.setBold(True)
        font.setLetterSpacing(QFont.PercentageSpacing, 110)
        lbl.setFont(font)
        return lbl

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
        show = bool(show) and self.movie.isValid()
        self.gif_lbl.setVisible(show)
        if show:
            if self.movie.state() != QMovie.Running:
                self.movie.start()
            QTimer.singleShot(0, self._rescale_movie)
        else:
            self.movie.stop()


__all__ = ["MainMenu"]
