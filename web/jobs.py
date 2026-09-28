"""Run schedule generation in the background for the web interface.

Generation takes seconds to minutes, far longer than a web request should
block, so it runs on a thread and the pages poll its progress. One run at a
time: two people on the same roster generating at once would only race to
apply competing schedules. Results live in memory and are gone after a
restart, which only costs re-running the generation; nothing is written to
the database until an option is approved.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from scheduler import (
    NurseManager,
    NurseScheduler,
    PreScheduler,
    SharedSettings,
    WeekendHistory,
    build_scheduler_from_settings,
)
from scheduler.engine import search_capped_note

logger = logging.getLogger(__name__)

# How many ranked options are kept for review, as in the desktop app.
TOP_OPTIONS = 5


@dataclass
class Job:
    """One generation run and, once it ends, its outcome.

    ``status`` is ``"running"``, then ``"done"``, ``"infeasible"``,
    ``"cancelled"`` or ``"failed"``. ``candidates`` holds the best
    :data:`TOP_OPTIONS` as ``(idx, stats, nurse_counts, schedule)``.
    """

    id: str
    start: date
    end: date
    allowed: list[str]
    status: str = "running"
    stage: str = "Preparing…"
    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    candidates: list = field(default_factory=list)
    engine: str = "variants"
    notice: str | None = None
    error: str | None = None
    applied: int | None = None
    cancel_requested: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> int:
        end = self.finished if self.finished is not None else time.monotonic()
        return int(end - self.started)


def _generate(job: Job, db_name: str) -> None:
    """The desktop app's generation pipeline, reporting into ``job``."""
    settings = SharedSettings()
    scheduler = build_scheduler_from_settings(
        job.start,
        job.end,
        NurseManager(db_name),
        WeekendHistory(db_name),
        PreScheduler(db_name),
        settings,
    )
    scheduler.set_allow_rotation_violations(bool(job.allowed))
    scheduler.set_nurses_allowed_rotation_violation(job.allowed)
    mode = (
        NurseScheduler.WeekendVariantMode.RELAXED_ALLOWED
        if job.allowed
        else NurseScheduler.WeekendVariantMode.STRICT_ONLY
    )

    def on_stage(text: str) -> None:
        if not job.cancel_requested:
            job.stage = text

    def on_progress(done: int, total: int) -> None:
        job.done, job.total = done, total

    run = scheduler.run_generation(
        weekend_variant_mode=mode,
        on_stage=on_stage,
        on_progress=on_progress,
        is_cancelled=lambda: job.cancel_requested,
    )
    job.engine = run.engine
    if run.search_capped:
        job.notice = search_capped_note(scheduler.config.max_weekend_variants)
    if run.status == "cancelled":
        job.status = "cancelled"
    elif run.status == "infeasible" or not run.candidates:
        job.status = "infeasible"
    else:
        job.candidates = list(run.candidates[:TOP_OPTIONS])
        job.status = "done"


class JobManager:
    """Starts generation runs and keeps the latest one for the pages to show."""

    def __init__(self, db_name: str, runner: Callable[[Job, str], None] = _generate):
        self.db_name = db_name
        self._runner = runner
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._latest: Job | None = None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    @property
    def latest(self) -> Job | None:
        return self._latest

    @property
    def running(self) -> Job | None:
        job = self._latest
        return job if job is not None and job.running else None

    def start(self, start: date, end: date, allowed: list[str]) -> Job:
        """Start a run, or raise ``RuntimeError`` while another is running."""
        with self._lock:
            if self.running is not None:
                raise RuntimeError("A schedule is already being generated.")
            job = Job(id=uuid.uuid4().hex[:12], start=start, end=end, allowed=list(allowed))
            # Only the latest run's results are kept; older ones are dropped
            # so a long-running server does not accumulate schedules.
            self._jobs = {job.id: job}
            self._latest = job
        threading.Thread(target=self._run, args=(job,), name="generate", daemon=True).start()
        return job

    def cancel(self, job: Job) -> None:
        if job.running:
            job.cancel_requested = True
            job.stage = "Cancelling… (finishing the current step)"

    def _run(self, job: Job) -> None:
        try:
            self._runner(job, self.db_name)
        except Exception:
            logger.exception("Schedule generation failed")
            job.error = traceback.format_exc()
            job.status = "failed"
        finally:
            if job.status == "running":
                job.status = "cancelled" if job.cancel_requested else "failed"
            job.finished = time.monotonic()


__all__ = ["Job", "JobManager", "TOP_OPTIONS"]
