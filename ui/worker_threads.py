"""QThread workers used by the GUI."""

from __future__ import annotations

import logging
import os
import traceback
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)

from PySide6.QtCore import QThread, Signal

from scheduler import (
    NurseManager,
    PreScheduler,
    WeekendHistory,
    _evaluate_variant_worker,
    _evaluate_variant_worker_profiled,
    default_worker_count,
)

logger = logging.getLogger(__name__)

_CANCEL_POLL_SECONDS = 0.5

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


def _terminate_pool(pool) -> None:
    """Stop a cancelled executor without waiting for in-flight variants.

    ``shutdown(cancel_futures=True)`` only drops queued work; a process
    pool's running workers would otherwise keep evaluating (minutes per
    variant) after the user pressed Cancel, so terminate them.
    """
    pool.shutdown(wait=False, cancel_futures=True)
    processes = getattr(pool, "_processes", None) or {}
    for proc in list(processes.values()):
        try:
            proc.terminate()
        except Exception:  # pragma: no cover - best effort
            logger.debug("Could not terminate worker process", exc_info=True)


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

            self.stage.emit("Building weekend rotation variants…")
            variants = sched.generate_all_weekend_variants(
                allow_rotation_violations=self.allow_rotation_violations
            )
            if self.isInterruptionRequested():
                self.cancelled.emit()
                return
            if not variants:
                self.finished.emit([], sched, wh)
                return

            total = len(variants)
            candidate_schedules = []
            profiling = self.profiling_enabled
            evaluate = _evaluate_variant_worker_profiled if profiling else _evaluate_variant_worker

            override_env = os.environ.get("NSCHED_FORCE_THREAD_POOL")
            override = _coerce_env_flag(override_env)
            detected_android = _is_android_platform()
            use_threads = detected_android if override is None else override
            maxw = (
                min(4, os.cpu_count() or 1, total)
                if use_threads
                else min(default_worker_count(), total)
            )
            Executor = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
            logger.info(
                "Evaluating %d variants with %s (%d workers, android=%s, override=%r)",
                total,
                Executor.__name__,
                maxw,
                detected_android,
                override_env,
            )

            self.stage.emit(f"Evaluating {total} variant{'s' if total != 1 else ''}…")
            self.progress.emit(0, total)
            cancelled = False
            try:
                pool = Executor(max_workers=maxw)
                try:
                    pending = {
                        pool.submit(evaluate, (i, v, sched.worker_tuning))
                        for i, v in enumerate(variants)
                    }
                    done = 0
                    while pending:
                        # Poll rather than block on the next result so Cancel
                        # takes effect within a fraction of a second.
                        finished, pending = wait(
                            pending, timeout=_CANCEL_POLL_SECONDS, return_when=FIRST_COMPLETED
                        )
                        if self.isInterruptionRequested():
                            cancelled = True
                            break
                        for fut in finished:
                            done += 1
                            try:
                                res = fut.result()
                            except Exception:
                                logger.exception("Evaluating a variant failed")
                                res = None
                            if res is not None:
                                if profiling:
                                    *res, metrics = res
                                    self.worker_metrics.append(metrics)
                                    res = tuple(res)
                                candidate_schedules.append(res)
                            self.progress.emit(done, total)
                finally:
                    if cancelled:
                        _terminate_pool(pool)
                    else:
                        pool.shutdown(wait=True)
            except Exception:
                logger.exception("Variant evaluation pool failed")
                candidate_schedules.clear()

            if cancelled:
                self.cancelled.emit()
                return

            if not candidate_schedules:
                logger.warning("No variant evaluated cleanly; ranking unevaluated variants")
                for i, var in enumerate(variants):
                    try:
                        df = var.state.schedule.copy()
                        counts = {}
                        for n in getattr(var, "nurses", []):
                            m = int((df["main"] == n).sum()) if "main" in df.columns else 0
                            b = int((df["backup"] == n).sum()) if "backup" in df.columns else 0
                            counts[n] = {"main": m, "backup": b, "total": m + b}
                        mains = list(counts[n]["main"] for n in counts) or [0]
                        backs = list(counts[n]["backup"] for n in counts) or [0]
                        stats = {
                            "gaps": int(df[["main", "backup"]].isna().sum().sum())
                            if not df.empty
                            else 0,
                            "balance_main": int(max(mains) - min(mains)) if len(mains) > 1 else 0,
                            "balance_backup": int(max(backs) - min(backs)) if len(backs) > 1 else 0,
                            "rotation_rep": int(getattr(var.state, "rotation_repeats", 0)),
                        }
                        candidate_schedules.append((i, stats, counts, df))
                    except Exception:
                        logger.exception("Building fallback stats for variant %d failed", i)

            if not candidate_schedules:
                self.finished.emit([], sched, wh)
                return

            self.stage.emit("Ranking variants…")
            try:
                sched._score_and_rank_variants(candidate_schedules)
            except Exception:
                logger.exception("Ranking variants failed; keeping evaluation order")

            if DEBUG_SAVE_VARIANTS:
                try:
                    sched._debug_variant_dump = _prepare_variant_debug_payload(
                        candidate_schedules,
                        sched,
                        wh,
                        self._start,
                    )
                except Exception:
                    logger.exception("Preparing the variant debug payload failed")

            self.all_candidates = list(candidate_schedules)
            self.finished.emit(candidate_schedules[:5], sched, wh)

        except Exception:
            logger.exception("Schedule generation failed")
            self.error.emit(traceback.format_exc())


__all__ = ["ScheduleProgressWorker", "RebuildViolationWorker"]
