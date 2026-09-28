"""The web interface: pages, roster edits, and generating then approving a schedule.

Generation itself is the engine's business and has its own tests; here a
stand-in runner hands back a fixed schedule, so these tests cover what the
web layer adds: the forms, the background job, and approving an option into
history through the same ``apply_schedule`` path as the desktop app.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pandas as pd
import pytest

pytest.importorskip("flask")

from scheduler import AssignmentHistory, NurseManager, PreScheduler, WeekendHistory  # noqa: E402
from scheduler.engine import GenerationRun, NurseScheduler  # noqa: E402
from tests.scheduling_fixtures import REGULAR, seed_db  # noqa: E402
from web import create_app  # noqa: E402
from web import jobs as web_jobs  # noqa: E402
from web.jobs import Job, JobManager  # noqa: E402

MONDAY = date(2026, 3, 2)


def _frame(start: date, days: int = 14) -> pd.DataFrame:
    """A schedule over ``days`` days, rotating the regular nurses."""
    index = pd.date_range(pd.Timestamp(start), periods=days, freq="D")
    main = [REGULAR[i % len(REGULAR)] for i in range(days)]
    backup = [REGULAR[(i + 1) % len(REGULAR)] for i in range(days)]
    return pd.DataFrame({"main": main, "backup": backup}, index=index)


def _stats() -> dict:
    return {
        "rotation_rep": 0,
        "gaps": 0,
        "unfillable": 0,
        "weighted_score": 0.25,
        "balance_main": 1,
        "balance_backup": 1,
    }


class _Runner:
    """Stands in for the engine: optionally waits, then finishes the job."""

    def __init__(self, *, outcome: str = "done", release: threading.Event | None = None):
        self.outcome = outcome
        self.release = release
        self.calls: list[Job] = []

    def __call__(self, job: Job, db_name: str) -> None:
        self.calls.append(job)
        job.stage = "Solving the whole month…"
        if self.release is not None:
            while not self.release.wait(0.01):
                if job.cancel_requested:
                    job.status = "cancelled"
                    return
        if self.outcome == "error":
            raise RuntimeError("solver exploded")
        if self.outcome == "infeasible":
            job.status = "infeasible"
            return
        frame = _frame(job.start, (job.end - job.start).days + 1)
        job.candidates = [(0, _stats(), {}, frame), (1, _stats(), {}, frame)]
        job.status = "done"


def _wait(job: Job, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not job.running, "the job did not finish"


@pytest.fixture
def db(tmp_path):
    return seed_db(
        tmp_path,
        weekends=[("2026-02-13", "A", "B"), ("2026-02-20", "C", "D")],
        time_off=[("A", "2026-03-10"), ("A", "2026-04-02")],
        pre_scheduled=[("2026-03-05", "E", "F")],
    )


@pytest.fixture
def runner():
    return _Runner()


@pytest.fixture
def client(db, runner):
    app = create_app(db, jobs=JobManager(db, runner=runner))
    app.config["TESTING"] = True
    return app.test_client()


def _nurse_id(db: str, name: str) -> int:
    return NurseManager(db).nurses[name]["nurse_id"]


# ─────────────────────────────── pages ───────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/?month=2026-03",
        "/?month=not-a-month",
        "/nurses",
        "/time-off",
        "/pinned",
        "/pinned?past=1",
        "/weekends",
        "/generate",
        "/healthz",
    ],
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200


def test_time_off_page_for_one_nurse(client, db):
    response = client.get(f"/time-off?nurse={_nurse_id(db, 'A')}&month=2026-03")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'value="2026-03-10" checked' in page
    assert 'value="2026-03-11" checked' not in page


def test_unknown_nurse_is_404(client):
    assert client.post("/nurses/9999/remove").status_code == 404


def test_pages_send_security_headers(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_changes_from_another_site_are_refused(client, db):
    response = client.post(
        "/nurses", data={"name": "Mallory"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert NurseManager(db).find_nurse("Mallory") is None
    ok = client.post("/nurses", data={"name": "Zed"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 302


# ─────────────────────────────── roster ───────────────────────────────


def test_add_nurse_rejects_duplicates_and_restores_removed(client, db):
    client.post("/nurses", data={"name": "  New   Nurse ", "late": "1"})
    nurse = NurseManager(db).find_nurse("New Nurse")
    assert nurse == {"name": "New Nurse", "is_active": True, "is_prn": False, "is_late_shift": True}

    page = client.post("/nurses", data={"name": "new nurse"}, follow_redirects=True)
    assert "already on the roster" in page.get_data(as_text=True)

    client.post(f"/nurses/{_nurse_id(db, 'New Nurse')}/remove")
    assert "New Nurse" not in NurseManager(db).get_nurses()
    page = client.post("/nurses", data={"name": "NEW NURSE", "prn": "1"}, follow_redirects=True)
    assert "restored" in page.get_data(as_text=True)
    assert NurseManager(db).find_nurse("New Nurse")["is_prn"] is True


def test_nurse_flags_are_saved(client, db):
    client.post(f"/nurses/{_nurse_id(db, 'B')}/flags", data={"prn": "1"})
    nurses = NurseManager(db)
    assert nurses.is_prn_nurse("B") and not nurses.is_late_shift_nurse("B")
    client.post(f"/nurses/{_nurse_id(db, 'B')}/flags", data={"late": "1"})
    nurses = NurseManager(db)
    assert not nurses.is_prn_nurse("B") and nurses.is_late_shift_nurse("B")


def test_saving_time_off_replaces_only_the_month_shown(client, db):
    client.post(
        f"/time-off/{_nurse_id(db, 'A')}",
        data={"month": "2026-03", "off": ["2026-03-20", "2026-03-21", "2026-05-01"]},
    )
    days = {d.date() for d in NurseManager(db).get_unavailable_dates("A")}
    # March's days are what was ticked (a day outside March is ignored);
    # April's day, not on the form, is kept.
    assert days == {date(2026, 3, 20), date(2026, 3, 21), date(2026, 4, 2)}


def test_pinning_validates_and_replaces(client, db):
    page = client.post(
        "/pinned", data={"date": "2026-03-06", "main": "A", "backup": "A"}, follow_redirects=True
    )
    assert "cannot be both Main and Backup" in page.get_data(as_text=True)
    page = client.post("/pinned", data={"date": "2026-03-06"}, follow_redirects=True)
    assert "Choose a Main nurse" in page.get_data(as_text=True)
    page = client.post(
        "/pinned", data={"date": "2026-03-06", "main": "Nobody"}, follow_redirects=True
    )
    assert "from the roster" in page.get_data(as_text=True)
    assert len(PreScheduler(db).get_assignments()) == 1

    client.post("/pinned", data={"date": "2026-03-05", "backup": "G", "note": "swap"})
    assert PreScheduler(db).get_assignments() == [("2026-03-05", None, "G", "swap")]
    client.post("/pinned/2026-03-05/remove")
    assert PreScheduler(db).get_assignments() == []


# ─────────────────────────────── generation ───────────────────────────────


def test_generate_rejects_a_backwards_range(client, runner):
    page = client.post("/generate", data={"start": "2026-03-10", "end": "2026-03-01"})
    assert "before the start date" in page.get_data(as_text=True)
    assert runner.calls == []


def test_generate_asks_first_when_a_weekend_is_split(client, runner):
    # A Saturday start cuts a weekend in two, so the range will be widened.
    page = client.post("/generate", data={"start": "2026-03-07", "end": "2026-03-29"})
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "so no weekend is split" in text
    assert 'name="confirmed"' in text
    assert runner.calls == []

    response = client.post(
        "/generate", data={"start": "2026-03-07", "end": "2026-03-29", "confirmed": "1"}
    )
    assert response.status_code == 302 and "/jobs/" in response.headers["Location"]
    assert len(runner.calls) == 1


def test_generate_lists_pinned_day_problems(client, db, runner):
    PreScheduler(db).add_assignment("2026-03-10", "A", None)  # A is off that day
    page = client.post("/generate", data={"start": "2026-03-02", "end": "2026-03-29"})
    text = page.get_data(as_text=True)
    assert "pinned days in this range look wrong" in text
    assert "Generate anyway" in text
    assert runner.calls == []


def test_generate_passes_only_regular_nurses_allowed_to_repeat(client, runner):
    client.post(
        "/generate",
        data={"start": "2026-03-02", "end": "2026-03-29", "allow": ["A", "P", "Nobody"]},
    )
    assert runner.calls[0].allowed == ["A"]


def test_generate_approve_records_history(client, db, runner):
    response = client.post("/generate", data={"start": "2026-03-02", "end": "2026-03-15"})
    job = runner.calls[0]
    _wait(job)
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Schedule options" in page and "Option 2" in page

    status = client.get(f"/jobs/{job.id}/status").get_json()
    assert status["status"] == "done"

    pdf = client.get(f"/jobs/{job.id}/options/1.pdf")
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")
    assert client.get(f"/jobs/{job.id}/options/3.pdf").status_code == 404

    response = client.post(f"/jobs/{job.id}/options/2/approve")
    assert response.status_code == 302 and "month=2026-03" in response.headers["Location"]
    assert job.applied == 2

    history = AssignmentHistory(db).get_history("2026-03-02", "2026-03-15")
    expected = _frame(MONDAY, 14)
    assert [(d, m, b) for d, m, b in history] == [
        (day.strftime("%Y-%m-%d"), row["main"], row["backup"]) for day, row in expected.iterrows()
    ]
    # Friday's Main and Backup become that weekend's FSF and SFS nurses.
    friday = pd.Timestamp("2026-03-06")
    assert WeekendHistory(db).get_assignment(friday) == tuple(expected.loc[friday])

    month = client.get("/?month=2026-03").get_data(as_text=True)
    assert "Printable PDF" in month
    pdf = client.get("/schedule.pdf?month=2026-03")
    assert pdf.data.startswith(b"%PDF")


def test_only_one_generation_runs_at_a_time(db):
    release = threading.Event()
    runner = _Runner(release=release)
    app = create_app(db, jobs=JobManager(db, runner=runner))
    client = app.test_client()
    form = {"start": "2026-03-02", "end": "2026-03-29"}
    client.post("/generate", data=form)
    job = runner.calls[0]

    assert "Generating a schedule" in client.get("/nurses").get_data(as_text=True)
    response = client.post("/generate", data=form)
    assert response.headers["Location"].endswith(f"/jobs/{job.id}")
    assert len(runner.calls) == 1
    with pytest.raises(RuntimeError):
        app.extensions["jobs"].start(MONDAY, MONDAY, [])

    client.post(f"/jobs/{job.id}/cancel")
    _wait(job)
    assert job.status == "cancelled"
    assert "Generation cancelled" in client.get(f"/jobs/{job.id}").get_data(as_text=True)
    release.set()


@pytest.mark.parametrize(
    ("outcome", "heading"),
    [("infeasible", "No workable schedule"), ("error", "Generation failed")],
)
def test_runs_without_options_say_why(db, outcome, heading):
    runner = _Runner(outcome=outcome)
    client = create_app(db, jobs=JobManager(db, runner=runner)).test_client()
    client.post("/generate", data={"start": "2026-03-02", "end": "2026-03-29"})
    job = runner.calls[0]
    _wait(job)
    assert heading in client.get(f"/jobs/{job.id}").get_data(as_text=True)
    assert client.post(f"/jobs/{job.id}/options/1/approve").status_code == 404
    if outcome == "error":
        assert "solver exploded" in job.error


def test_default_range_starts_after_the_approved_schedule(client, db):
    ahead = date.today() + timedelta(days=3)
    AssignmentHistory(db).update_history(ahead.isoformat(), "A", "B")
    page = client.get("/generate").get_data(as_text=True)
    start = ahead + timedelta(days=1)
    assert f'value="{start.isoformat()}"' in page
    assert f'value="{(start + timedelta(days=27)).isoformat()}"' in page


def test_generate_runs_the_engine_pipeline(db, monkeypatch):
    """The real runner hands the run's settings to the engine and keeps its results."""
    seen = {}
    frame = _frame(MONDAY, 28)

    def fake_run_generation(self, **kwargs):
        seen["mode"] = kwargs["weekend_variant_mode"]
        seen["allowed"] = list(self.nurses_allowed_rotation_violation)
        kwargs["on_stage"]("Solving the whole month…")
        kwargs["on_progress"](1, 5)
        candidates = [(i, _stats(), {}, frame) for i in range(7)]
        return GenerationRun(status="ok", candidates=candidates, engine="month")

    monkeypatch.setattr(NurseScheduler, "run_generation", fake_run_generation)
    job = Job(id="x", start=MONDAY, end=MONDAY + timedelta(days=27), allowed=["C"])
    web_jobs._generate(job, db)

    assert seen == {"mode": NurseScheduler.WeekendVariantMode.RELAXED_ALLOWED, "allowed": ["C"]}
    assert job.status == "done" and job.engine == "month"
    assert len(job.candidates) == web_jobs.TOP_OPTIONS
    assert (job.done, job.total) == (1, 5)
