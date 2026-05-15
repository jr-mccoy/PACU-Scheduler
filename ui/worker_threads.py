"""QThread workers used by the GUI."""

from __future__ import annotations

import os
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from PySide6.QtCore import QThread, Signal

from scheduler import (
    NurseManager,
    PreScheduler,
    WeekendHistory,
    _evaluate_variant_worker,
)

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
    progress = Signal(int, int)
    finished = Signal(object, object, object)
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

        # Normalize legacy midweek toggles into the master flag
        try:
            if "allow_one_day_weekday_gap" not in self.settings:
                a = bool(self.settings.get("allow_midweek_pair_backup_only", False))
                b = bool(self.settings.get("allow_midweek_pair_mixed", False))
                self.settings["allow_one_day_weekday_gap"] = a or b
        except Exception:
            pass

    def run(self):
        # Imported lazily to keep module import lightweight and avoid cycles
        # with ui.legacy during module load.
        from .legacy import apply_backend_debug_preferences
        from .presenters.variant_review_presenter import (
            _prepare_variant_debug_payload,
        )

        try:
            apply_backend_debug_preferences(self.settings)
            nm = NurseManager(DB_NAME)
            wh = WeekendHistory(DB_NAME)
            ps = PreScheduler(DB_NAME)

            from scheduler import build_scheduler_from_settings

            sched = build_scheduler_from_settings(
                self._start, self._end, nm, wh, ps, self.settings
            )

            sched.set_allow_rotation_violations(self.allow_rotation_violations)
            sched.set_nurses_allowed_rotation_violation(
                self.nurses_allowed_rotation_violation or []
            )

            variants = sched.generate_all_weekend_variants(
                allow_rotation_violations=self.allow_rotation_violations
            )
            if not variants:
                self.finished.emit([], sched, wh)
                return

            total = len(variants)
            candidate_schedules = []

            override_env = os.environ.get("NSCHED_FORCE_THREAD_POOL")
            override = _coerce_env_flag(override_env)
            detected_android = _is_android_platform()
            use_threads = detected_android if override is None else override
            maxw = (
                min(4, os.cpu_count() or 1, total)
                if use_threads
                else min(8, os.cpu_count() or 1, total)
            )
            Executor = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
            print(
                "[progress-worker] using"
                f" {'ThreadPoolExecutor' if use_threads else 'ProcessPoolExecutor'}"
                f" (android={detected_android}, override={override_env!r})"
            )

            try:
                with Executor(max_workers=maxw) as pool:
                    futures = [
                        pool.submit(_evaluate_variant_worker, (i, v))
                        for i, v in enumerate(variants)
                    ]
                    for done, fut in enumerate(as_completed(futures), start=1):
                        try:
                            res = fut.result()
                            if res is not None:
                                candidate_schedules.append(res)
                        except Exception:
                            pass
                        self.progress.emit(done, total)
            except Exception:
                candidate_schedules.clear()

            if not candidate_schedules:
                for i, var in enumerate(variants):
                    try:
                        df = var.state.schedule.copy()
                        counts = {}
                        for n in getattr(var, "nurses", []):
                            m = (
                                int((df["main"] == n).sum())
                                if "main" in df.columns
                                else 0
                            )
                            b = (
                                int((df["backup"] == n).sum())
                                if "backup" in df.columns
                                else 0
                            )
                            counts[n] = {"main": m, "backup": b, "total": m + b}
                        mains = list(counts[n]["main"] for n in counts) or [0]
                        backs = list(counts[n]["backup"] for n in counts) or [0]
                        stats = {
                            "gaps": int(df[["main", "backup"]].isna().sum().sum())
                            if not df.empty
                            else 0,
                            "balance_main": int(max(mains) - min(mains))
                            if len(mains) > 1
                            else 0,
                            "balance_backup": int(max(backs) - min(backs))
                            if len(backs) > 1
                            else 0,
                            "rotation_rep": int(getattr(var.state, "rotation_repeats", 0)),
                        }
                        candidate_schedules.append((i, stats, counts, df))
                    except Exception:
                        pass

            if not candidate_schedules:
                self.finished.emit([], sched, wh)
                return

            try:
                sched._score_and_rank_variants(candidate_schedules)
            except Exception:
                pass

            if DEBUG_SAVE_VARIANTS:
                try:
                    sched._debug_variant_dump = _prepare_variant_debug_payload(
                        candidate_schedules,
                        sched,
                        wh,
                        self._start,
                    )
                except Exception:
                    pass

            self.finished.emit(candidate_schedules[:5], sched, wh)

        except Exception:
            self.error.emit(traceback.format_exc())


__all__ = ["ScheduleProgressWorker", "RebuildViolationWorker"]
