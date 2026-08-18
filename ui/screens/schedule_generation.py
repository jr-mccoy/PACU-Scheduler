"""Schedule generation screen — runs schedule generation in a background worker."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scheduler import AssignmentHistory, WeekendHistory

from ..config import DB_NAME
from ..dialogs.rotation_violation_dialog import RotationViolationDialog
from ..dialogs.variant_review_dialog import VariantReviewDialog
from ..messages import show_error, show_info, show_warning
from ..style import UiStyle
from ..widgets.date_pickers import MultiDatePicker, SingleDatePicker
from ..worker_threads import ScheduleProgressWorker


class ScheduleGenerationScreen(QWidget):
    """
    Lets the user choose start / end dates (matching SingleDatePicker look),
    then runs schedule generation in a background thread, with per-nurse rotation violation control.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        accent = parent.settings.get("accent_color")
        theme = parent.settings.get("theme")

        # start / end pickers -------------------------------------------------
        for label_txt, attr in [("Start Date", "_start_cal"), ("End Date", "_end_cal")]:
            lbl = QLabel(label_txt, alignment=Qt.AlignCenter)
            lbl.setFont(UiStyle.TITLE_FONT)
            lay.addWidget(lbl)

            picker = SingleDatePicker(accent=accent, initial=QDate.currentDate(), theme=theme)
            lay.addWidget(picker)
            setattr(self, attr, picker)

        gen_btn = QPushButton("Generate Schedule")
        gen_btn.setFont(QFont("Roboto", 16))
        gen_btn.setMinimumHeight(48)
        gen_btn.clicked.connect(self._on_generate)
        lay.addWidget(gen_btn)

        back = QPushButton("Back", minimumHeight=48)
        back.setProperty("role", "special")
        back.setFont(QFont("Roboto", 16))
        back.clicked.connect(lambda: parent.switch_frame("main"))
        lay.addWidget(back)

        # placeholders
        self.wh = self.ah = self.backup = None
        self._progress = None
        self.worker = None
        self._variant_dialog = None

    def _on_generate(self):
        s_iso = self._start_cal.iso()
        e_iso = self._end_cal.iso()
        start = datetime.strptime(s_iso, "%Y-%m-%d").date()
        end = datetime.strptime(e_iso, "%Y-%m-%d").date()
        if end < start:
            show_warning(self, "Error", "End date is before start date")
            return

        # backup histories ---------------------------------------------------
        self.wh, self.ah = WeekendHistory(DB_NAME), AssignmentHistory(DB_NAME)
        self.backup = self.wh.backup()

        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        dlg = RotationViolationDialog(self, self.parent.backend, accent=accent, theme=theme)

        def after_dialog(accepted):
            if not accepted:
                return
            allow, nurses_allowed = dlg.get_values()

            # Now show progress dialog and launch worker
            if self._progress:
                self._progress.cancel()
            self._progress = QProgressDialog("Preparing…", None, 0, 100, self)
            self._progress.setWindowTitle("Generating Schedules")
            self._progress.setWindowModality(Qt.WindowModal)
            self._progress.setCancelButton(None)
            self._progress.setMinimumDuration(0)
            self._progress.setValue(0)
            self._progress.setFixedSize(320, 120)
            if theme == "dark":
                dlg_bg = "#2D3238"
                text = "#E8EAF0"
                bar_bg = "#3A404B"
            elif theme == "light":
                dlg_bg = "#F8F6F3"
                text = "#2C2A27"
                bar_bg = "#FFFFFF"
            else:  # pink
                dlg_bg = "#F28AAC"
                text = "#FFFFFF"
                bar_bg = "#FFFFFF"
            self._progress.setStyleSheet(f"""
                QProgressDialog {{
                    background-color:{dlg_bg};
                    border:2px solid {accent};
                    border-radius:16px;
                    padding:10px; font-size:16px; color:{text};
                }}
                QProgressDialog QLabel {{ color:{text}; }}
                QProgressBar {{
                    height:20px; border-radius:10px;
                    background:{bar_bg}; text-align:center;
                    border:1px solid {accent};
                }}
                QProgressBar::chunk {{
                    background:{accent}; border-radius:10px;
                }}
            """)
            self._progress.show()

            # Launch worker with allow/nurses_allowed
            self.worker = ScheduleProgressWorker(
                start,
                end,
                allow_rotation_violations=allow,
                nurses_allowed_rotation_violation=nurses_allowed,
                settings=self.parent.settings,
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.error.connect(self._on_worker_error)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.start()

        dlg.accepted.connect(lambda: after_dialog(True))
        dlg.rejected.connect(lambda: after_dialog(False))
        dlg.open()

    def _on_progress(self, done: int, total: int):
        pct = int(100 * done / total) if total else 0
        self._progress.setMaximum(100)
        self._progress.setValue(pct)
        self._progress.setLabelText(f"Processing {done}/{total}  ({pct}%)")

    def _on_worker_error(self, msg: str):
        self._progress.cancel()
        show_error(self, "Error", msg)
        if self.wh and self.backup:
            self.wh.restore(self.backup)

    def apply_theme_update(self) -> None:
        """Apply theme and accent to calendar pickers when theme changes."""
        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        for picker in self.findChildren(MultiDatePicker):
            picker.set_theme(theme, accent)
        for picker in self.findChildren(SingleDatePicker):
            picker.set_theme(theme, accent)

    def _on_worker_finished(self, variants, scheduler, wh):
        from ..services.variant_export import _save_outputs_for_variants

        # hide progress bar
        if self._progress:
            self._progress.cancel()

        # Guard: no feasible candidates
        if not variants:
            show_info(
                self,
                "No feasible schedules",
                "No valid schedules were generated for this date range and constraints.",
            )
            if self.wh and self.backup:
                self.wh.restore(self.backup)
            return

        # ALWAYS save HTML + PDFs to a timestamped folder.
        try:
            out_dir = _save_outputs_for_variants(variants, scheduler, top_n=min(5, len(variants)))
            show_info(
                self,
                "Schedules exported",
                f"Saved calendar HTML and PDFs to:\n{out_dir}\n\n"
                "Open them manually to review. You can still use the in-app review now.",
            )
        except Exception as e:
            show_warning(self, "Export failed", f"Could not save schedules:\n{e}")

        # Proceed to the normal review dialog
        self._variant_dialog = VariantReviewDialog(self.parent, variants, wh, self.ah, self.backup)
        self._variant_dialog.rejected.connect(lambda: self.wh.restore(self.backup))
        self._variant_dialog.open()


__all__ = ["ScheduleGenerationScreen"]
