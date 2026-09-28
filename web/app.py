"""The PACU scheduler as a small web app, for phones and other computers.

It uses the same ``scheduler`` package, database and settings file as the
desktop app, so the two can be used side by side on one machine. It has no
login of its own: it is meant to listen on localhost and be reached through
a private network such as Tailscale (see ``docs/web-server.md``).
"""

from __future__ import annotations

import calendar
import logging
import os
import tempfile
from datetime import date, timedelta

import pandas as pd
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from scheduler import (
    AssignmentHistory,
    NurseManager,
    NurseScheduler,
    PreScheduler,
    SharedSettings,
    WeekendHistory,
    apply_schedule,
    build_scheduler_from_settings,
    ensure_schema,
)
from scheduler.engine import recorded_weekends_in_range, whole_weekend_range
from scheduler.exporters.pdf import export_variant_pdf

from . import present
from .jobs import JobManager

logger = logging.getLogger(__name__)

DEFAULT_SPAN_DAYS = 28  # four weeks, start day included, as in the desktop app

# Browsers label a request made by another site's page "cross-site" (or
# "same-site" for a sibling subdomain). Refusing those for every change stops
# another page from submitting forms here with the viewer's access.
_FOREIGN_FETCH_SITES = {"cross-site", "same-site"}

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(db_name: str = "nurse_schedule.db", jobs: JobManager | None = None) -> Flask:
    """The web app over the database at ``db_name``."""
    ensure_schema(db_name)
    app = Flask(__name__)
    # Only signs the one-off messages shown after a change, so a new key per
    # start is enough.
    app.secret_key = os.urandom(32)
    app.config["DB"] = db_name
    app.extensions["jobs"] = jobs or JobManager(db_name)

    app.jinja_env.filters["day"] = lambda d: d.strftime("%a %b %d").replace(" 0", " ")
    app.jinja_env.globals.update(
        METRICS=present.METRICS, metric=present.metric, elapsed=present.elapsed
    )

    @app.before_request
    def refuse_foreign_changes():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("Sec-Fetch-Site", "") in _FOREIGN_FETCH_SITES:
                abort(403)

    @app.after_request
    def security_headers(response: Response) -> Response:
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.context_processor
    def running_job():
        return {"running_job": app.extensions["jobs"].running, "today": date.today()}

    _register_schedule_routes(app)
    _register_roster_routes(app)
    _register_generation_routes(app)

    @app.get("/healthz")
    def healthz():
        return "ok"

    return app


# ─────────────────────────────── helpers ───────────────────────────────


def _db() -> str:
    return current_app.config["DB"]


def _jobs() -> JobManager:
    return current_app.extensions["jobs"]


def _roster() -> list[dict]:
    """Active nurses as ``{"id", "name", "prn", "late"}``, by name."""
    nurses = NurseManager(_db()).nurses
    return sorted(
        (
            {
                "id": info["nurse_id"],
                "name": name,
                "prn": info["is_prn"],
                "late": info["is_late_shift"],
            }
            for name, info in nurses.items()
        ),
        key=lambda n: n["name"].casefold(),
    )


def _nurse_or_404(nurse_id: int) -> dict:
    for nurse in _roster():
        if nurse["id"] == nurse_id:
            return nurse
    abort(404)


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _pdf_response(schedule: pd.DataFrame, filename: str) -> Response:
    """``schedule`` as the desktop app's printable month-grid PDF."""
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        export_variant_pdf(
            path, schedule, calendar.Calendar(firstweekday=6), NurseScheduler.PDF_FONT_SIZES
        )
        with open(path, "rb") as f:
            data = f.read()
    finally:
        os.unlink(path)
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ─────────────────────────────── schedule ───────────────────────────────


def _register_schedule_routes(app: Flask) -> None:
    def month_days(month: str | None) -> tuple[int, int, list[present.Day]]:
        year, mon = present.parse_month(month, date.today())
        first, last = present.month_bounds(year, mon)
        records = AssignmentHistory(_db()).get_history(first, last)
        return year, mon, present.days_from_history(records, first, last)

    @app.get("/")
    def schedule():
        year, mon, days = month_days(request.args.get("month"))
        return render_template(
            "schedule.html",
            title=f"{calendar.month_name[mon]} {year}",
            month=f"{year:04d}-{mon:02d}",
            prev_month=present.shift_month(year, mon, -1),
            next_month=present.shift_month(year, mon, 1),
            weeks=present.weeks(days),
            counts=present.counts(days),
            any_filled=any(d.filled for d in days),
        )

    @app.get("/schedule.pdf")
    def schedule_pdf():
        year, mon, days = month_days(request.args.get("month"))
        return _pdf_response(present.frame_from_days(days), f"schedule_{year:04d}-{mon:02d}.pdf")

    @app.get("/weekends")
    def weekends():
        wh = WeekendHistory(_db())
        rows = sorted(wh.get_assignments(), key=lambda r: r[0], reverse=True)
        regular = [n for n in _roster() if not n["prn"]]
        summary = wh.get_violation_summary()
        stats = {row["nurse"]: row for _, row in summary.iterrows()}
        nurses = []
        for n in regular:
            last = wh.get_last_pattern(n["name"])
            row = stats.get(n["name"])
            nurses.append(
                {
                    "name": n["name"],
                    "last": last.value if last is not None else None,
                    "violations": int(row["total_viol"]) if row is not None else 0,
                    "streak": int(row["consec_viol"]) if row is not None else 0,
                }
            )
        return render_template(
            "weekends.html",
            title="Weekend history",
            rows=[(pd.Timestamp(f).date(), fsf, sfs) for f, fsf, sfs in rows],
            nurses=nurses,
        )


# ─────────────────────────────── roster ───────────────────────────────


def _register_roster_routes(app: Flask) -> None:
    @app.get("/nurses")
    def nurses():
        return render_template("nurses.html", title="Nurses", nurses=_roster())

    @app.post("/nurses")
    def add_nurse():
        name = " ".join(request.form.get("name", "").split())
        prn = bool(request.form.get("prn"))
        late = bool(request.form.get("late"))
        if not name:
            flash("Enter the nurse's name.", "error")
            return redirect(url_for("nurses"))
        nm = NurseManager(_db())
        existing = nm.find_nurse(name)
        if existing and existing["is_active"]:
            flash(f"{existing['name']} is already on the roster.", "error")
            return redirect(url_for("nurses"))
        if existing:
            # A previously removed nurse comes back with their history, under
            # the name as stored, rather than as what looks like a new person.
            name = existing["name"]
            flash(f"{name} was removed earlier and is restored, with their history.", "ok")
        else:
            flash(f"Added {name}.", "ok")
        nm.add_nurse(name, is_prn=prn, is_late_shift=late)
        return redirect(url_for("nurses"))

    @app.post("/nurses/<int:nurse_id>/flags")
    def nurse_flags(nurse_id: int):
        nurse = _nurse_or_404(nurse_id)
        nm = NurseManager(_db())
        nm.set_prn_status(nurse["name"], bool(request.form.get("prn")))
        nm.set_late_shift_status(nurse["name"], bool(request.form.get("late")))
        flash(f"Saved {nurse['name']}.", "ok")
        return redirect(url_for("nurses"))

    @app.post("/nurses/<int:nurse_id>/remove")
    def remove_nurse(nurse_id: int):
        nurse = _nurse_or_404(nurse_id)
        NurseManager(_db()).remove_nurse(nurse["name"])
        flash(f"Removed {nurse['name']}. Past assignments and weekend history are kept.", "ok")
        return redirect(url_for("nurses"))

    @app.get("/time-off")
    def time_off():
        roster = _roster()
        nurse_id = request.args.get("nurse", type=int)
        nurse = next((n for n in roster if n["id"] == nurse_id), None)
        nm = NurseManager(_db())
        today = date.today()
        if nurse is None:
            upcoming = []
            for n in roster:
                days = sorted(
                    d.date() for d in nm.get_unavailable_dates(n["name"]) if d.date() >= today
                )
                if days:
                    upcoming.append((n, days))
            return render_template(
                "time_off.html", title="Time off", roster=roster, nurse=None, upcoming=upcoming
            )
        year, mon = present.parse_month(request.args.get("month"), today)
        off = {d.date() for d in nm.get_unavailable_dates(nurse["name"])}
        return render_template(
            "time_off.html",
            title=f"Time off · {nurse['name']}",
            roster=roster,
            nurse=nurse,
            month=f"{year:04d}-{mon:02d}",
            month_title=f"{calendar.month_name[mon]} {year}",
            prev_month=present.shift_month(year, mon, -1),
            next_month=present.shift_month(year, mon, 1),
            grid=present.month_grid(year, mon),
            off=off,
            upcoming=sorted(d for d in off if d >= today),
        )

    @app.post("/time-off/<int:nurse_id>")
    def save_time_off(nurse_id: int):
        nurse = _nurse_or_404(nurse_id)
        year, mon = present.parse_month(request.form.get("month"), date.today())
        first, last = present.month_bounds(year, mon)
        picked = {d for d in map(_parse_day, request.form.getlist("off")) if d}
        picked = {d for d in picked if first <= d <= last}
        nm = NurseManager(_db())
        # The form shows one month: days in other months are kept as they are.
        kept = {d.date() for d in nm.get_unavailable_dates(nurse["name"])}
        kept = {d for d in kept if not first <= d <= last}
        nm.update_unavailable_dates(nurse["name"], {pd.Timestamp(d) for d in kept | picked})
        flash(
            f"Saved {nurse['name']}'s time off for {calendar.month_name[mon]} "
            f"({len(picked)} day{'s' if len(picked) != 1 else ''}).",
            "ok",
        )
        return redirect(url_for("time_off", nurse=nurse_id, month=f"{year:04d}-{mon:02d}"))

    @app.get("/pinned")
    def pinned():
        today = date.today()
        show_past = bool(request.args.get("past"))
        rows = []
        for day, main, backup, note in PreScheduler(_db()).get_assignments():
            d = _parse_day(str(day)[:10])
            if d is not None and (show_past or d >= today):
                rows.append((d, main, backup, note))
        rows.sort()
        return render_template(
            "pinned.html",
            title="Pre-scheduled days",
            rows=rows,
            roster=_roster(),
            show_past=show_past,
        )

    @app.post("/pinned")
    def add_pinned():
        day = _parse_day(request.form.get("date"))
        main = request.form.get("main") or None
        backup = request.form.get("backup") or None
        note = request.form.get("note", "").strip()
        names = {n["name"] for n in _roster()}
        if day is None:
            flash("Pick a date.", "error")
        elif not main and not backup:
            flash("Choose a Main nurse, a Backup nurse, or both.", "error")
        elif (main and main not in names) or (backup and backup not in names):
            flash("Choose nurses from the roster.", "error")
        elif main and main == backup:
            flash(f"{main} cannot be both Main and Backup on the same day.", "error")
        else:
            PreScheduler(_db()).add_assignment(day.isoformat(), main, backup, note)
            flash(f"Pinned {day:%a %b %d}.", "ok")
        return redirect(url_for("pinned"))

    @app.post("/pinned/<day>/remove")
    def remove_pinned(day: str):
        parsed = _parse_day(day)
        if parsed is None:
            abort(404)
        PreScheduler(_db()).remove_assignment(parsed.isoformat())
        flash(f"Removed the pin on {parsed:%a %b %d}.", "ok")
        return redirect(url_for("pinned"))


# ─────────────────────────────── generation ───────────────────────────────


def _rotation_choices() -> list[dict]:
    """Nurses who may be allowed a rotation repeat, fewest past repeats first."""
    wh = WeekendHistory(_db())
    regular = {n["name"] for n in _roster() if not n["prn"]}
    summary = wh.get_violation_summary()
    summary = summary[summary["nurse"].isin(regular)].sort_values(
        by=["total_viol", "consec_viol", "clean_run_weeks", "days_since_last"],
        ascending=[True, True, False, False],
    )
    out = []
    for _, row in summary.iterrows():
        viol, clean = int(row["total_viol"]), int(row["clean_run_weeks"])
        detail = (
            "never repeated"
            if clean >= 999
            else f"{viol} repeat{'s' if viol != 1 else ''}, {clean} clean weekend"
            f"{'s' if clean != 1 else ''} since"
        )
        out.append({"name": row["nurse"], "detail": detail})
    return out


def _default_range() -> tuple[date, date]:
    """Four weeks from the day after the last approved day, or from today."""
    start = date.today()
    ahead = AssignmentHistory(_db()).get_history(start_date=start)
    if ahead:
        start = date.fromisoformat(ahead[-1][0][:10]) + timedelta(days=1)
    return start, start + timedelta(days=DEFAULT_SPAN_DAYS - 1)


def _register_generation_routes(app: Flask) -> None:
    def form_page(start: date, end: date, allowed: list[str], **extra):
        return render_template(
            "generate.html",
            title="Generate a schedule",
            start=start,
            end=end,
            allowed=set(allowed),
            choices=_rotation_choices(),
            has_nurses=bool(_roster()),
            **extra,
        )

    @app.get("/generate")
    def generate():
        start, end = _default_range()
        return form_page(start, end, [])

    @app.post("/generate")
    def start_generation():
        start = _parse_day(request.form.get("start"))
        end = _parse_day(request.form.get("end"))
        regular = {n["name"] for n in _roster() if not n["prn"]}
        allowed = [n for n in request.form.getlist("allow") if n in regular]
        if start is None or end is None:
            return form_page(*_default_range(), allowed, error="Pick a start and end date.")
        if end < start:
            return form_page(start, end, allowed, error="The end date is before the start date.")
        if not regular:
            return form_page(start, end, allowed, error="Add nurses to the roster first.")
        running = _jobs().running
        if running is not None:
            flash("A schedule is already being generated. It is shown below.", "error")
            return redirect(url_for("job", job_id=running.id))

        if not request.form.get("confirmed"):
            wh = WeekendHistory(_db())
            first, last = whole_weekend_range(
                pd.Timestamp(start),
                pd.Timestamp(end),
                is_recorded=lambda friday: wh.get_assignment(friday) is not None,
            )
            first, last = first.date(), last.date()
            replaces = len(recorded_weekends_in_range(wh, first, last))
            try:
                scheduler = build_scheduler_from_settings(
                    start, end, NurseManager(_db()), wh, PreScheduler(_db()), SharedSettings()
                )
                issues = [issue.message for issue in scheduler.validate_pre_schedule()]
            except Exception:
                logger.exception("Checking the pre-schedule failed")
                issues = []
            widened = (first, last) != (start, end)
            if issues or widened or replaces:
                return form_page(
                    start,
                    end,
                    allowed,
                    review={
                        "span": present.span(first, last) if widened else None,
                        "replaces": replaces,
                        "issues": issues,
                    },
                )

        job = _jobs().start(start, end, allowed)
        return redirect(url_for("job", job_id=job.id))

    def job_or_404(job_id: str):
        job = _jobs().get(job_id)
        if job is None:
            abort(404)
        return job

    @app.get("/jobs/latest")
    def latest_job():
        job = _jobs().latest
        if job is None:
            return redirect(url_for("generate"))
        return redirect(url_for("job", job_id=job.id))

    @app.get("/jobs/<job_id>")
    def job(job_id: str):
        job = job_or_404(job_id)
        options = []
        for rank, (_idx, stats, _counts, frame) in enumerate(job.candidates, start=1):
            days = present.days_from_frame(frame)
            options.append(
                {
                    "rank": rank,
                    "stats": stats,
                    "weeks": present.weeks(days),
                    "counts": present.counts(days),
                    "span": present.span(days[0].day, days[-1].day) if days else "",
                }
            )
        chosen = request.args.get("option", type=int) or job.applied or 1
        chosen = min(max(chosen, 1), max(len(options), 1))
        return render_template(
            "job.html",
            title="Generated schedules" if job.status == "done" else "Generating",
            job=job,
            options=options,
            chosen=chosen,
            requested=present.span(job.start, job.end),
        )

    @app.get("/jobs/<job_id>/status")
    def job_status(job_id: str):
        job = job_or_404(job_id)
        return jsonify(
            status=job.status,
            stage=job.stage,
            done=job.done,
            total=job.total,
            elapsed=present.elapsed(job.elapsed),
        )

    @app.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job = job_or_404(job_id)
        _jobs().cancel(job)
        return redirect(url_for("job", job_id=job.id))

    def option_frame(job, rank: int) -> pd.DataFrame:
        if job.status != "done" or not 1 <= rank <= len(job.candidates):
            abort(404)
        return job.candidates[rank - 1][3]

    @app.get("/jobs/<job_id>/options/<int:rank>.pdf")
    def option_pdf(job_id: str, rank: int):
        frame = option_frame(job_or_404(job_id), rank)
        return _pdf_response(frame, f"schedule_option_{rank}.pdf")

    @app.post("/jobs/<job_id>/options/<int:rank>/approve")
    def approve_option(job_id: str, rank: int):
        job = job_or_404(job_id)
        frame = option_frame(job, rank)
        try:
            report = apply_schedule(_db(), frame)
        except Exception as exc:
            logger.exception("Approving option %d failed", rank)
            flash(f"Option {rank} was not approved, so nothing changed: {exc}", "error")
            return redirect(url_for("job", job_id=job.id, option=rank))
        job.applied = rank
        first = pd.Timestamp(frame.index.min()).date()
        flash(
            f"Option {rank} approved: {report.days_recorded} days and "
            f"{report.weekends_recorded} weekend rotations recorded.",
            "ok",
        )
        return redirect(url_for("schedule", month=f"{first:%Y-%m}"))


__all__ = ["create_app"]
