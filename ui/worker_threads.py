"""QThread workers used by the GUI."""

from __future__ import annotations

import logging
import os
import traceback

from PySide6.QtCore import QThread, Signal

from scheduler import (
    NurseManager,
    NurseScheduler,
    PreScheduler,
    WeekendHistory,
)

logger = logging.getLogger(__name__)

DB_NAME = "nurse_schedule.db"
DEBUG_SAVE_VARIANTS = bool(int(os.environ.get("NSCHED_DEBUG_VARIANTS", "0")))


def _coerce_env_flag(value):
    """Map common truthy/falsey strings to bool; return None when unknown."""
    if value is None:
        return None
    val = value.strip().lower()
    if val in {"1", "true", "yes", "on", "t", "threads", "thread"}:
        return True
    if val in {"0", "false", "no", "off", "f", "process", "processes", "proc"}:
        return False
    return None


def _is_android_platform() -> bool:
    """Return True when running inside any Android/Python-for-Android build."""
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


class RebuildViolationWorker(QThread):
    finished = Signal(bool, str)  # (success, message)

    def __init__(self, weekend_history, violation_service=None):
        super().__init__()
        self.weekend_history = weekend_history
        self.violation_service = violation_service or getattr(
            weekend_history, "violation_service", None
        )

    def run(self):
        try:
            if self.violation_service is not None:
                self.violation_service.rebuild()
            else:
                self.weekend_history._recalculate_violation_counts()
            self.finished.emit(True, "Violation history rebuilt.")
        except Exception as e:
            self.finished.emit(False, f"Error: {e}")


class ScheduleProgressWorker(QThread):
    """Generate and evaluate schedule variants off the GUI thread.

    Signals:
        stage(str)            human-readable description of the current step
        progress(done, total) variants evaluated so far
        finished(top5, scheduler, weekend_history)
        cancelled()           the user cancelled; no results
        error(str)            a traceback, for the details pane and the log

    After ``finished``, ``all_candidates`` holds every evaluated variant and
    ``worker_metrics`` the per-variant profiling data (when
    ``measure_phase_times`` is on) for the diagnostics exports.
    """

    progress = Signal(int, int)
    stage = Signal(str)
    finished = Signal(object, object, object)
    cancelled = Signal()
    error = Signal(str)

    def __init__(
        self,
        start_date,
        end_date,
        *,
        allow_rotation_violations=False,
        nurses_allowed_rotation_violation=None,
        settings=None,
    ):
        super().__init__()
        self._start = start_date
        self._end = end_date
        self.allow_rotation_violations = allow_rotation_violations
        self.nurses_allowed_rotation_violation = nurses_allowed_rotation_violation or []
        try:
            if hasattr(settings, "all"):
                self.settings = settings.all()
            elif isinstance(settings, dict):
                self.settings = dict(settings)
            else:
                self.settings = {}
        except Exception:
            self.settings = {}
        self.all_candidates: list = []
        self.worker_metrics: list = []

    @property
    def profiling_enabled(self) -> bool:
        return bool(self.settings.get("measure_phase_times"))

    def _use_threads(self) -> bool:
        """Threads on Android (no working process pools), or when forced."""
        override = _coerce_env_flag(os.environ.get("NSCHED_FORCE_THREAD_POOL"))
        return _is_android_platform() if override is None else override

    def run(self):
        # Imported lazily to keep module import lightweight and avoid cycles
        # with ui.legacy during module load.
        from .platform import apply_backend_debug_preferences
        from .presenters.variant_review_presenter import (
            _prepare_variant_debug_payload,
        )

        try:
            apply_backend_debug_preferences(self.settings)
            nm = NurseManager(DB_NAME)
            wh = WeekendHistory(DB_NAME)
            ps = PreScheduler(DB_NAME)

            from scheduler import build_scheduler_from_settings

            sched = build_scheduler_from_settings(self._start, self._end, nm, wh, ps, self.settings)

            sched.set_allow_rotation_violations(self.allow_rotation_violations)
            sched.set_nurses_allowed_rotation_violation(
                self.nurses_allowed_rotation_violation or []
            )

            # The same pipeline the CLI runs; this worker only bridges its
            # callbacks to Qt signals.
            use_threads = self._use_threads()
            mode = (
                NurseScheduler.WeekendVariantMode.RELAXED_ALLOWED
                if self.allow_rotation_violations
                else NurseScheduler.WeekendVariantMode.STRICT_ONLY
            )
            run = sched.run_generation(
                max_workers=min(4, os.cpu_count() or 1) if use_threads else None,
                weekend_variant_mode=mode,
                profile=self.profiling_enabled,
                use_threads=use_threads,
                on_stage=self.stage.emit,
                on_progress=self.progress.emit,
                is_cancelled=self.isInterruptionRequested,
            )

            if run.status == "cancelled":
                self.cancelled.emit()
                return
            if run.status == "infeasible":
                self.finished.emit([], sched, wh)
                return

            self.worker_metrics = list(run.worker_metrics)
            if DEBUG_SAVE_VARIANTS:
                try:
                    sched._debug_variant_dump = _prepare_variant_debug_payload(
                        run.candidates,
                        sched,
                        wh,
                        self._start,
                    )
                except Exception:
                    logger.exception("Preparing the variant debug payload failed")

            self.all_candidates = list(run.candidates)
            self.finished.emit(run.candidates[:5], sched, wh)

        except Exception:
            logger.exception("Schedule generation failed")
            self.error.emit(traceback.format_exc())


__all__ = ["ScheduleProgressWorker", "RebuildViolationWorker"]
