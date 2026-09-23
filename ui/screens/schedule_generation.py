"""Schedule generation screen — runs schedule generation in a background worker."""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import pandas as pd
from PySide6.QtCore import QDate, QElapsedTimer, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QLabel,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scheduler import AssignmentHistory, WeekendHistory
from scheduler.engine import (
    recorded_weekends_in_range,
    search_capped_note,
    whole_weekend_range,
)
from scheduler.exporters import write_gap_report

from ..config import DB_NAME, DEBUG_SAVE_VARIANTS
from ..dialogs.rotation_violation_dialog import RotationViolationDialog
from ..dialogs.variant_review_dialog import VariantReviewDialog
from ..messages import show_error, show_info
from ..widgets.common import action_button, back_button, screen_title
from ..widgets.date_pickers import MultiDatePicker, SingleDatePicker
from ..worker_threads import ScheduleProgressWorker

logger = logging.getLogger(__name__)

DEFAULT_SPAN_DAYS = 28  # four weeks, start day included


def describe_range(
    start: date,
    end: date,
    scheduled: tuple[date, date] | None = None,
    replaces: int = 0,
) -> str:
    """Summary of a scheduling horizon, e.g. for the screen footer.

    ``scheduled`` is the range the scheduler will actually cover, which is
    wider when ``start``..``end`` cuts a weekend in two (see
    :func:`scheduler.engine.whole_weekend_range`). Weekends are counted over
    it, and a second line says why it differs. ``replaces`` is how many
    weekends are already recorded in that range; applying an option will
    replace them.
    """
    if end < start:
        return ""
    first_day, last_day = scheduled or (start, end)
    days = (end - start).days + 1
    weekends = sum(
        1
        for i in range((last_day - first_day).days + 1)
        if (first_day + timedelta(days=i)).weekday() == 5
    )
    text = (
        f"{_span(start, end)}  ·  {days} day{'s' if days != 1 else ''}"
        f"  ·  {weekends} weekend{'s' if weekends != 1 else ''}"
    )
    if (first_day, last_day) != (start, end):
        text += f"\nScheduling {_span(first_day, last_day)} so no weekend is split."
    if replaces:
        text += (
            f"\n{replaces} weekend{'s are' if replaces != 1 else ' is'} already recorded in "
            "this range. The new schedule is built without them and replaces them when applied."
        )
    return text


def _span(start: date, end: date) -> str:
    first = start.strftime("%a %b %d") if start.year == end.year else start.strftime("%a %b %d, %Y")
    return f"{first} – {end.strftime('%a %b %d, %Y')}"


def _qdate_to_date(qd: QDate) -> date:
    return date(qd.year(), qd.month(), qd.day())


class ScheduleGenerationScreen(QWidget):
    """Pick a start and end date, then generate and review candidate schedules."""

    # Side by side when there is room for two calendars, stacked otherwise.
    _SIDE_BY_SIDE_MIN_WIDTH = 960

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        lay.addWidget(screen_title("Generate Schedule"))

        settings = parent.settings
        accent = settings.get("accent_color")
        theme = settings.get("theme")
        grid = bool(settings.get("calendar_grid"))

        today = QDate.currentDate()
        self._pickers = QBoxLayout(QBoxLayout.LeftToRight)
        self._pickers.setSpacing(24)
        for label_txt, attr, initial in [
            ("Start date", "_start_cal", today),
            ("End date", "_end_cal", today.addDays(DEFAULT_SPAN_DAYS - 1)),
        ]:
            col = QVBoxLayout()
            col.setSpacing(6)
            lbl = QLabel(label_txt, alignment=Qt.AlignCenter)
            font = lbl.font()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 2)
            lbl.setFont(font)
            col.addWidget(lbl)
            picker = SingleDatePicker(accent=accent, initial=initial, theme=theme, grid=grid)
            picker.dateChanged.connect(self._update_summary)
            col.addWidget(picker, 1)
            self._pickers.addLayout(col, 1)
            setattr(self, attr, picker)
        lay.addLayout(self._pickers, 1)

        self.summary = QLabel(alignment=Qt.AlignCenter)
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        self.gen_btn = action_button(
            "Generate Schedule", "special", tooltip="Build and rank candidate schedules"
        )
        self.gen_btn.clicked.connect(self._on_generate)
        lay.addWidget(self.gen_btn)

        lay.addWidget(back_button(self, parent))

        # placeholders
        self._running = False
        self.ah = None
        self._weekend_history: WeekendHistory | None = None
        self._progress: QProgressDialog | None = None
        self.worker: ScheduleProgressWorker | None = None
        self._variant_dialog = None
        self._stage_text = ""
        self._done = self._total = 0
        self._elapsed = QElapsedTimer()
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_progress_label)

        self._update_summary()

    # ─────────────────────────── range selection ───────────────────────────
    def selected_range(self) -> tuple[date, date]:
        return _qdate_to_date(self._start_cal.qdate()), _qdate_to_date(self._end_cal.qdate())

    def on_show(self):
        """Re-read weekend history, which decides how edge weekends are handled."""
        self._weekend_history = None
        self._update_summary()

    def _scheduled_range(self, start: date, end: date) -> tuple[date, date]:
        """The range generation will cover, with split weekends made whole."""
        if self._weekend_history is None:
            self._weekend_history = WeekendHistory(DB_NAME)
        history = self._weekend_history
        first, last = whole_weekend_range(
            pd.Timestamp(start),
            pd.Timestamp(end),
            is_recorded=lambda friday: history.get_assignment(friday) is not None,
        )
        return first.date(), last.date()

    def _update_summary(self, *_):
        start, end = self.selected_range()
        running = self._running
        if end < start:
            self.summary.setText("The end date is before the start date. Pick a later end date.")
            self.summary.setProperty("role", "error")
            self.gen_btn.setEnabled(False)
        else:
            first, last = self._scheduled_range(start, end)
            replaces = len(recorded_weekends_in_range(self._weekend_history, first, last))
            self.summary.setText(describe_range(start, end, (first, last), replaces))
            self.summary.setProperty("role", None)
            self.gen_btn.setEnabled(not running)
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        direction = (
            QBoxLayout.LeftToRight
            if self.width() >= self._SIDE_BY_SIDE_MIN_WIDTH
            else QBoxLayout.TopToBottom
        )
        if self._pickers.direction() != direction:
            self._pickers.setDirection(direction)

    # ─────────────────────────── generation ───────────────────────────
    def _on_generate(self):
        start, end = self.selected_range()
        if end < start or self._running:
            return

        # Generation only reads history; nothing is written until an option
        # is applied, so there is nothing to back up or restore here.
        self.ah = AssignmentHistory(DB_NAME)

        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")

        dlg = RotationViolationDialog(self, self.parent.backend, accent=accent, theme=theme)

        def after_dialog(accepted):
            if not accepted:
                return
            allow, nurses_allowed = dlg.get_values()
            self._start_worker(start, end, allow, nurses_allowed)

        dlg.accepted.connect(lambda: after_dialog(True))
        dlg.rejected.connect(lambda: after_dialog(False))
        dlg.open()

    def _start_worker(self, start: date, end: date, allow: bool, nurses_allowed: list[str]):
        self._open_progress()
        self.worker = ScheduleProgressWorker(
            start,
            end,
            allow_rotation_violations=allow,
            nurses_allowed_rotation_violation=nurses_allowed,
            settings=self.parent.settings,
        )
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
        self.worker.error.connect(self._on_worker_error)
        self.worker.cancelled.connect(self._on_worker_cancelled)
        self.worker.finished.connect(self._on_worker_finished)
        self._running = True
        self._elapsed.start()
        self._tick.start()
        self._update_summary()
        self.worker.start()

    def _open_progress(self):
        if self._progress:
            self._progress.reset()
            self._progress.close()
        theme = self.parent.settings.get("theme")
        accent = self.parent.settings.get("accent_color")
        self._stage_text = "Preparing…"
        self._done = self._total = 0

        progress = QProgressDialog(self._stage_text, "Cancel", 0, 0, self)
        self._cancel_btn = QPushButton("Cancel")
        progress.setCancelButton(self._cancel_btn)
        progress.setWindowTitle("Generating Schedules")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(420)
        if theme == "dark":
            dlg_bg, text, bar_bg = "#2D3238", "#E8EAF0", "#3A404B"
        elif theme == "light":
            dlg_bg, text, bar_bg = "#F8F6F3", "#2C2A27", "#FFFFFF"
        else:  # pink
            dlg_bg, text, bar_bg = "#FDEDEE", "#4A4A4A", "#FFFFFF"
        progress.setStyleSheet(f"""
            QProgressDialog {{
                background-color:{dlg_bg};
                border:2px solid {accent};
                border-radius:16px;
                padding:10px; font-size:15px; color:{text};
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
        progress.canceled.connect(self._on_cancel_requested)
        progress.show()
        self._progress = progress

    def _on_cancel_requested(self):
        if self.worker is None or not self._running:
            return
        self.worker.requestInterruption()
        self._stage_text = "Cancelling… (finishing the current step)"
        if self._progress:
            # QProgressDialog hides itself on cancel; keep it up until the
            # worker actually stops so the screen cannot start a second run.
            self._cancel_btn.setText("Cancelling…")
            self._cancel_btn.setEnabled(False)
            self._progress.show()
        self._refresh_progress_label()

    def _on_stage(self, text: str):
        if self.worker is not None and self.worker.isInterruptionRequested():
            return
        self._stage_text = text
        self._refresh_progress_label()

    def _on_progress(self, done: int, total: int):
        self._done, self._total = done, total
        if self._progress:
            self._progress.setRange(0, max(total, 1))
            self._progress.setValue(done)
        self._refresh_progress_label()

    def _refresh_progress_label(self):
        if not self._progress:
            return
        secs = self._elapsed.elapsed() // 1000 if self._elapsed.isValid() else 0
        elapsed = f"{secs // 60}:{secs % 60:02d}"
        lines = [self._stage_text]
        if self._total:
            lines.append(f"{self._done} of {self._total} evaluated  ·  elapsed {elapsed}")
        else:
            lines.append(f"Elapsed {elapsed}")
        self._progress.setLabelText("\n".join(lines))

    def _close_progress(self):
        self._tick.stop()
        if self._progress:
            self._progress.reset()
            self._progress.close()
            self._progress = None

    def _finish_run(self):
        self._running = False
        self._close_progress()
        self._update_summary()

    def _on_worker_cancelled(self):
        self._finish_run()
        show_info(self, "Generation cancelled", "No schedule was generated. Nothing was changed.")

    def _on_worker_error(self, trace: str):
        self._finish_run()
        show_error(
            self,
            "Schedule generation failed",
            "Something went wrong while generating schedules, so nothing was changed. "
            "Use “Show details” for the technical error to include in a bug report.",
            details=trace,
        )

    def apply_theme_update(self) -> None:
        """Apply theme and accent to calendar pickers when theme changes."""
        settings = self.parent.settings
        theme = settings.get("theme")
        accent = settings.get("accent_color")
        grid = bool(settings.get("calendar_grid"))

        for picker in self.findChildren(MultiDatePicker):
            picker.set_theme(theme, accent, grid=grid)
        for picker in self.findChildren(SingleDatePicker):
            picker.set_theme(theme, accent, grid=grid)

    def _on_worker_finished(self, variants, scheduler, wh):
        from ..services.variant_export import _save_outputs_for_variants

        # Guard: no feasible candidates
        if not variants:
            self._finish_run()
            capped = (
                "The weekend search was capped this run, so raising “Weekend variants to "
                "evaluate” is the first thing to try.\n\n"
                if self.worker is not None and self.worker.search_capped
                else ""
            )
            show_info(
                self,
                "No feasible schedules",
                capped + "No schedule satisfies every rule for this date range. Things to try:\n\n"
                "• Raise “Weekend variants to evaluate” in Settings (or set it to "
                "Unlimited); a low cap can prune the only workable weekend pattern.\n"
                "• Allow rotation violations for some nurses when you generate.\n"
                "• Enable a post-weekend or one-day-gap relaxation in Settings.\n"
                "• Check for conflicting pre-scheduled assignments or time off.",
            )
            return

        # Save HTML + PDFs to a timestamped folder; the review dialog links to it.
        self._on_stage("Saving PDF and HTML copies…")
        QApplication.processEvents()
        failures: list[str] = []
        out_dir = None
        export_error = None
        try:
            out_dir = _save_outputs_for_variants(
                variants,
                scheduler,
                top_n=min(5, len(variants)),
                debug_save_variants=DEBUG_SAVE_VARIANTS,
                failures=failures,
            )
            if failures:
                export_error = "; ".join(failures)
        except Exception as e:
            logger.exception("Exporting schedules failed")
            export_error = str(e)

        if out_dir:
            self._write_diagnostics(scheduler, out_dir)

        self._finish_run()

        # Proceed to the review dialog
        self._variant_dialog = VariantReviewDialog(
            self.parent,
            variants,
            wh,
            self.ah,
            out_dir=out_dir,
            export_error=export_error,
            notice=self._capped_note(scheduler),
        )
        # Applying records weekends in this range; refresh the summary's count.
        self._variant_dialog.accepted.connect(self.on_show)
        self._variant_dialog.open()

    def _capped_note(self, scheduler) -> str | None:
        """The beam-capped notice for this run, if the cap discarded branches."""
        if self.worker is None or not self.worker.search_capped:
            return None
        return search_capped_note(scheduler.config.max_weekend_variants)

    def _write_diagnostics(self, scheduler, out_dir: str) -> None:
        """Write the opt-in diagnostics from Settings next to the exported PDFs."""
        settings = self.parent.settings
        worker = self.worker
        if worker is None:
            return
        if settings.get("measure_phase_times") and worker.worker_metrics:
            try:
                scheduler._report_performance_metrics(
                    worker.worker_metrics, os.path.join(out_dir, "performance_metrics.json")
                )
            except Exception:
                logger.exception("Writing performance metrics failed")
        if settings.get("analyse_initial_weekday_gaps") and worker.all_candidates:
            name = settings.get("gap_report_file") or "weekday_gap_report.txt"
            path = name if os.path.isabs(name) else os.path.join(out_dir, name)
            try:
                write_gap_report(worker.all_candidates, path)
            except Exception:
                logger.exception("Writing the weekday gap report failed")


__all__ = ["ScheduleGenerationScreen", "describe_range"]
