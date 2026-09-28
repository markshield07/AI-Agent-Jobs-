"""One apply pass: queued jobs with a ready resume, through the browser.

    job + variant -> packet -> handler fills the form -> attempt recorded

The mode decides how far a job goes. `dry_run` fills everything and stops at
the submit button, leaving a screenshot and the list of what it filled;
`review` does the same and parks the application for approval; `auto`
submits, under a daily cap and with a pause between submissions (jobpilot's
campaign limits). A form that asks what nobody on file can answer stops
before the button whatever the mode, and the questions come back to be
answered once. Every try is kept, so a dry run can be read before the mode is
changed, and a job is never submitted twice.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from jobagent.answers import list_answers, set_answer
from jobagent.apply import sessions, store
from jobagent.apply.answering import make_answerer
from jobagent.apply.browser.fill import wait_settled
from jobagent.apply.browser.session import BrowserSession, BrowserUnavailable, open_browser
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.models import MODES, Handler, HandlerResult, NeededInput, Packet
from jobagent.apply.sessions import SITES
from jobagent.config import Settings
from jobagent.db.database import utcnow
from jobagent.discovery import store as jobs
from jobagent.discovery.ats import detect_ats
from jobagent.discovery.criteria import load_criteria
from jobagent.discovery.scoring.rules import not_remote, place_matches
from jobagent.llm.backend import Completer, LLMUnavailable, resolve_backend
from jobagent.resume.facts import list_facts, list_never_claim
from jobagent.tailor import store as variants
from jobagent.tailor.models import Variant
from jobagent.tailor.render import cover_letter_html, write_pdf

log = logging.getLogger(__name__)

CONTACT_KEYS = ("full_name", "first_name", "last_name", "email", "phone", "location")
LINK_KEYS = ("linkedin", "github", "website", "portfolio")
# Where a job goes on the jobs table after an attempt. A dry run or review
# puts it back in the queue, so a job parked on a question returns once the
# question is answered and the form fills through.
_JOB_STATUS_FOR = {
    "submitted": "applied",
    "dry_run": "queued",
    "review": "queued",
    "needs_input": "needs_review",
    "blocked": "needs_review",
    "unconfirmed": "needs_review",
    "failed": "failed",
}


class ApplyError(RuntimeError):
    """The job cannot be applied to as things stand."""


@dataclass(slots=True)
class ApplyReport:
    """What one pass did, for the CLI and the dashboard."""

    run_id: int | None
    mode: str
    considered: int = 0
    attempted: int = 0
    submitted: int = 0
    dry_run: int = 0
    review: int = 0
    needs_input: int = 0
    blocked: int = 0
    unconfirmed: int = 0
    failed: int = 0
    skipped: int = 0
    cap_hit: bool = False
    # Sites and Workday companies whose sign-in a run met, with how many jobs wait on each.
    sign_ins: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"mode {self.mode}", f"considered {self.considered}"]
        for name in (
            "submitted",
            "dry_run",
            "review",
            "needs_input",
            "blocked",
            "unconfirmed",
            "failed",
        ):
            count = getattr(self, name)
            if count:
                parts.append(f"{name.replace('_', ' ')} {count}")
        if self.skipped:
            parts.append(f"skipped {self.skipped}")
        if self.cap_hit:
            parts.append("daily cap reached")
        for where, count in self.sign_ins.items():
            parts.append(f"needs sign-in: {where} ({count} waiting)")
        if self.input_tokens or self.output_tokens:
            parts.append(f"tokens {self.input_tokens} in / {self.output_tokens} out")
        return ", ".join(parts)


# ---------------------------------------------------------------- packet --


def _links_from_answers(answers: Mapping[str, str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for key in LINK_KEYS:
        if answers.get(key):
            links[key] = answers[key].strip()
    for url in (answers.get("links") or "").split(","):
        url = url.strip()
        if not url:
            continue
        host = url.lower()
        if "linkedin.com" in host:
            links.setdefault("linkedin", url)
        elif "github.com" in host:
            links.setdefault("github", url)
        else:
            links.setdefault("website", url)
    return links


def _cover_letter_pdf(
    variant: Variant, job: Mapping[str, Any], contact: Mapping[str, str], settings: Settings
) -> str | None:
    """The letter as a PDF beside the resume, rendered once and reused."""
    if variant.cover_letter is None or variant.id is None:
        return None
    path = settings.variants_dir / f"{variant.job_id}-{variant.id}-letter.pdf"
    if not path.is_file():
        html = cover_letter_html(
            variant.cover_letter,
            contact,
            company=job.get("company"),
            job_title=job.get("title"),
        )
        write_pdf(html, path)
    return str(path)


def build_packet(
    conn: sqlite3.Connection, job: Mapping[str, Any], variant: Variant, settings: Settings
) -> Packet:
    """Everything the form may be filled from, gathered once per job."""
    answers = {row["key"]: row["value"] for row in list_answers(conn)}
    contact = {key: answers[key] for key in CONTACT_KEYS if answers.get(key)}
    letter = variant.cover_letter
    return Packet(
        job=dict(job),
        contact=contact,
        resume_path=variant.pdf_path or "",
        cover_letter="\n\n".join(letter.paragraphs) if letter else None,
        cover_letter_path=_cover_letter_pdf(variant, job, contact, settings),
        links=_links_from_answers(answers),
        answers=answers,
        facts=list_facts(conn),
        never_claim=[row["term"] for row in list_never_claim(conn)],
        variant_id=variant.id,
        wanted_places=wanted_places(conn, job),
    )


def wanted_places(conn: sqlite3.Connection, job: Mapping[str, Any] | None = None) -> list[str]:
    """The locations searched for, lower case; empty when none are set. With
    the job, also the place its listing gave when a board found it searching
    around a wanted place ("irvine, ca" for Orange County), so the posting
    may put it there."""
    criteria = load_criteria(conn)
    wanted = criteria.normalised(criteria.locations)
    near = str((job or {}).get("found_near") or "").strip().lower()
    listed = str((job or {}).get("location") or "").strip().lower()
    if near and listed and listed not in wanted and any(place_matches(w, near) for w in wanted):
        wanted.append(listed)
    return wanted


# ------------------------------------------------------------------- run --


def _resolve_completer(
    settings: Settings, completer: Completer | None, notes: list[str]
) -> Completer | None:
    if completer is not None or not settings.apply_model_answers:
        return completer
    try:
        return resolve_backend(settings)
    except LLMUnavailable as exc:
        notes.append(f"no model for open questions: {exc}")
        return None


def _skipped(job_id: str, reason: str, application_id: int | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "application_id": application_id,
        "outcome": "skipped",
        "reason": reason,
    }


def _workday_signed_out(settings: Settings, url: str) -> str | None:
    """The company's Workday host when its sign-in is known not to work (never
    made, or met a sign-in page since): its jobs wait instead of opening a
    browser only to stop at the same page."""
    try:
        host = sessions.workday_host(url)
    except sessions.UnknownSite:
        return None
    state = sessions.workday_status(settings, host)["state"]
    return host if state in ("not_signed_in", "needs_sign_in") else None


def _workday_wait_note(settings: Settings, host: str) -> str:
    command = sessions.workday_status(settings, host)["login_command"]
    return (
        f"waiting for a Workday sign-in at {host}; run `{command}` on your own machine "
        "and it is tried again right after"
    )


def apply_to_job(
    conn: sqlite3.Connection,
    job_id: str,
    settings: Settings,
    *,
    mode: str | None = None,
    completer: Completer | None = None,
    browser: BrowserSession | None = None,
    handlers: Sequence[Handler] | None = None,
    allow_generic: bool = True,
) -> dict[str, Any]:
    """Take one job through its form. Returns a record of what happened; raises
    ApplyError only when there is no such job, the mode is unknown, or the
    browser cannot start."""
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise ApplyError(f"No job {job_id}.")
    mode = mode or settings.apply_mode
    if mode not in MODES:
        raise ApplyError(f"unknown mode {mode!r}")
    if store.job_is_applied(conn, job_id):
        app = store.application_for_job(conn, job_id)
        return _skipped(job_id, "already applied", app["id"] if app else None)

    url = job.get("apply_url") or job.get("url") or ""
    # Queued before the rules knew better, or the places wanted changed since.
    office = not_remote(job, load_criteria(conn))
    if office:
        return _wrong_place(conn, settings, job_id, url, office)

    variant = variants.latest_ready_variant(conn, job_id)
    if variant is None or not variant.pdf_path or not Path(variant.pdf_path).is_file():
        return _skipped(job_id, "no ready resume for this job; run tailor first")

    ats = job.get("ats_type") or detect_ats(url)
    handlers = list(handlers) if handlers is not None else default_handlers(generic=allow_generic)
    handler = handler_for(url, ats, handlers)
    if handler is None:
        return _skipped(job_id, f"no handler for {ats or 'this site'}")
    waiting_on = _workday_signed_out(settings, url) if handler.ats == "workday" else None
    if waiting_on:
        # The posting is public: no sign-in is asked for a job in the wrong place.
        elsewhere = _posting_elsewhere(conn, settings, browser, handler, url, job)
        if elsewhere:
            return _wrong_place(conn, settings, job_id, url, elsewhere)
        sessions.mark_workday_signed_out(settings, waiting_on, job_id=job_id, url=url)
        app = store.application_for_job(conn, job_id)
        return {
            **_skipped(
                job_id, _workday_wait_note(settings, waiting_on), app["id"] if app else None
            ),
            "sign_in": waiting_on,
        }
    submit = mode == "auto"
    if submit and handler.ats in SITES:
        sent = store.submitted_last_day(conn, ats=handler.ats)
        if sent >= settings.easy_apply_daily_cap:
            return _skipped(
                job_id,
                f"{SITES[handler.ats].label} cap of {settings.easy_apply_daily_cap} "
                "applications a day reached; it goes out tomorrow",
            )

    notes: list[str] = []
    completer = _resolve_completer(settings, completer, notes)
    packet = build_packet(conn, job, variant, settings)
    answerer = make_answerer(
        packet,
        completer=completer,
        allow_model=settings.apply_model_answers,
        # Auto mode is the person's own choice to send applications all the way.
        agree_to_terms=mode == "auto",
    )
    application_id = store.get_or_create_application(
        conn,
        job_id,
        mode=mode,
        variant_id=variant.id,
        ats=handler.ats if handler.ats != "generic" else ats,
        cover_letter_path=packet.cover_letter_path,
    )
    attempt_no = len(store.list_attempts(conn, application_id)) + 1
    screenshot = settings.screenshots_dir / f"{job_id}-{attempt_no}.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)

    started = utcnow()
    first_ats = handler.ats
    try:
        session_cm = nullcontext(browser) if browser is not None else open_browser(settings)
        with session_cm as session, session.new_page() as page:
            result = handler.apply(
                page, packet, answerer, submit=submit, screenshot_path=str(screenshot)
            )
            onward = _company_site_handler(result, handler, handlers)
            if onward is not None:
                onward_url = result.external_url or ""
                notes.append(f"{handler.ats} sends this job to {onward_url}")
                handler = onward
                packet = replace(packet, job={**packet.job, "apply_url": onward_url})
                waiting_on = (
                    _workday_signed_out(settings, onward_url) if handler.ats == "workday" else None
                )
                if waiting_on:
                    result = HandlerResult(
                        outcome="blocked",
                        error=_workday_wait_note(settings, waiting_on),
                        sign_in=waiting_on,
                    )
                else:
                    result = handler.apply(
                        page, packet, answerer, submit=submit, screenshot_path=str(screenshot)
                    )
    except BrowserUnavailable as exc:  # not the job's fault: nothing is recorded
        raise ApplyError(str(exc)) from exc
    except Exception as exc:  # a handler bug is a failed attempt, not a dead run
        log.exception("handler %s failed on %s", handler.ats, job_id)
        result = HandlerResult(outcome="failed", error=f"{type(exc).__name__}: {exc}")

    if handler.ats != first_ats:
        # Sent on to the company's site: it no longer counts toward the site's own cap.
        onward_url = packet.job.get("apply_url") or ""
        store.get_or_create_application(
            conn,
            job_id,
            mode=mode,
            ats=handler.ats if handler.ats != "generic" else detect_ats(onward_url),
        )

    if result.wrong_place:
        return {
            **_wrong_place(
                conn, settings, job_id, packet.job.get("apply_url") or url, result.wrong_place
            ),
            "notes": notes,
        }

    if result.outcome == "dry_run" and mode == "review":
        result.outcome = "review"
    if result.outcome == "submitted" and not submit:
        notes.append("the handler reported a submission in a mode that does not submit")
    if handler.ats == "workday":
        _note_workday_sign_in(settings, job_id, packet.job.get("apply_url") or url, result)
    attempt_id = store.record_attempt(
        conn, application_id, result, mode=mode, handler=handler.ats, started_at=started
    )
    new_status = _JOB_STATUS_FOR.get(result.outcome)
    if new_status:
        store.mark_job(conn, job_id, new_status)

    return {
        "job_id": job_id,
        "application_id": application_id,
        "attempt_id": attempt_id,
        "outcome": result.outcome,
        "handler": handler.ats,
        "error": result.error,
        "confirmation": result.confirmation,
        "needed": [n.as_dict() for n in result.needed],
        "filled": len(result.filled),
        "screenshot_path": result.screenshot_path,
        "sign_in": result.sign_in,
        "notes": notes,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def _posting_elsewhere(
    conn: sqlite3.Connection,
    settings: Settings,
    browser: BrowserSession | None,
    handler: Handler,
    url: str,
    job: Mapping[str, Any] | None = None,
) -> str | None:
    """Open the posting only to read where it is; None when that is fine or unknown."""
    wanted = wanted_places(conn, job)
    check = getattr(handler, "wrong_place", None)
    if not wanted or check is None:
        return None
    goto = getattr(handler, "application_url", lambda u: u)(url)
    try:
        session_cm = nullcontext(browser) if browser is not None else open_browser(settings)
        with session_cm as session, session.new_page() as page:
            page.goto(goto, wait_until="domcontentloaded", timeout=30_000)
            wait_settled(page, getattr(handler, "settle_ms", 8_000))
            return check(page, wanted)
    except Exception as exc:  # unknown: the job waits for the sign-in as before
        log.info("could not read where %s is: %s", url, exc)
        return None


def _wrong_place(
    conn: sqlite3.Connection, settings: Settings, job_id: str, url: str, reason: str
) -> dict[str, Any]:
    """Skip the job for good, with the reason, and take it off any sign-in wait."""
    jobs.set_status(conn, job_id, "skipped", reason)
    store.withdraw_unsent(conn, job_id, reason)
    try:
        sessions.clear_workday_waiting(settings, sessions.workday_host(url), [job_id])
    except sessions.UnknownSite:
        pass
    return {**_skipped(job_id, reason), "wrong_place": True}


def _note_workday_sign_in(settings: Settings, job_id: str, url: str, result: HandlerResult) -> None:
    """What a Workday run showed about the company's sign-in: a sign-in page
    marks it expired and parks the job until the next sign-in; getting into
    the form proves it still works."""
    if result.sign_in:
        if _is_workday_host(result.sign_in):
            sessions.mark_workday_signed_out(settings, result.sign_in, job_id=job_id, url=url)
        return
    if result.outcome in ("dry_run", "review", "needs_input", "submitted", "unconfirmed"):
        try:
            host = sessions.workday_host(url)
        except sessions.UnknownSite:
            return
        sessions.record_workday_check(settings, host, "signed_in")
        sessions.clear_workday_waiting(settings, host, [job_id])


def _company_site_handler(
    result: HandlerResult, handler: Handler, handlers: Sequence[Handler]
) -> Handler | None:
    """The handler for the company's own form, when a LinkedIn or Indeed posting
    sends the applicant there; None when there is nowhere to go or nothing
    that fills that site's form."""
    url = result.external_url
    if result.outcome != "blocked" or not url or handler.ats not in SITES:
        return None
    others = [h for h in handlers if h.ats not in SITES]
    return handler_for(url, detect_ats(url), others)


def _candidates(conn: sqlite3.Connection, job_ids: Iterable[str] | None, limit: int) -> list[str]:
    if job_ids:
        return list(dict.fromkeys(job_ids))
    picked: list[str] = []
    for job in jobs.list_jobs(conn, status="queued", limit=max(limit * 5, 50)):
        if variants.latest_ready_variant(conn, job["id"]) is None:
            continue
        picked.append(job["id"])
        if len(picked) >= limit:
            break
    return picked


def run_apply(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    job_ids: Iterable[str] | None = None,
    limit: int = 10,
    mode: str | None = None,
    completer: Completer | None = None,
    browser: BrowserSession | None = None,
    handlers: Sequence[Handler] | None = None,
    allow_generic: bool = True,
    delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ApplyReport:
    """Apply to `job_ids`, or to queued jobs with a ready resume, `limit` at most."""
    mode = mode or settings.apply_mode
    if mode not in MODES:
        raise ApplyError(f"unknown mode {mode!r}")
    submit = mode == "auto"
    delay = settings.apply_delay_seconds if delay is None else delay
    report = ApplyReport(run_id=None, mode=mode)
    candidates = _candidates(conn, job_ids, limit)
    report.considered = len(candidates)
    if not candidates:
        report.notes.append("nothing to apply to: no queued job has a ready resume")
        return report

    completer = _resolve_completer(settings, completer, report.notes)
    handlers = list(handlers) if handlers is not None else default_handlers(generic=allow_generic)
    report.run_id = jobs.start_run(conn)
    try:
        session_cm = nullcontext(browser) if browser is not None else open_browser(settings)
        with session_cm as session:
            for index, job_id in enumerate(candidates):
                if submit and store.submitted_last_day(conn) >= settings.daily_apply_cap:
                    report.cap_hit = True
                    report.notes.append(
                        f"daily cap of {settings.daily_apply_cap} reached; "
                        f"{len(candidates) - index} left for tomorrow"
                    )
                    break
                record = apply_to_job(
                    conn,
                    job_id,
                    settings,
                    mode=mode,
                    completer=completer,
                    browser=session,
                    handlers=handlers,
                )
                _tally(report, record)
                if submit and delay > 0 and index < len(candidates) - 1:
                    sleep(delay * random.uniform(0.7, 1.3))
    except BrowserUnavailable as exc:
        report.notes.append(str(exc))
    finally:
        for where in report.sign_ins:
            report.notes.append(_sign_in_note(settings, where))
        jobs.finish_run(
            conn,
            report.run_id,
            applied=report.submitted,
            failed=report.failed,
            tokens_in=report.input_tokens,
            tokens_out=report.output_tokens,
        )
    return report


def _is_workday_host(host: str) -> bool:
    try:
        return sessions.workday_host(f"https://{host}/") == host
    except sessions.UnknownSite:
        return False


def _sign_in_note(settings: Settings, where: str) -> str:
    if where in SITES:
        return f"{SITES[where].label} needs you to sign in again: run `jobagent login {where}`"
    if not _is_workday_host(where):
        return f"{where} needs you to sign in again"
    status = sessions.workday_status(settings, where)
    return (
        f"Workday at {where} needs you to sign in again: run `{status['login_command']}`; "
        f"{status['waiting']} application(s) wait for it and run right after"
    )


def _tally(report: ApplyReport, record: Mapping[str, Any]) -> None:
    report.results.append(dict(record))
    if record.get("sign_in"):
        where = str(record["sign_in"])
        report.sign_ins[where] = report.sign_ins.get(where, 0) + 1
    outcome = record["outcome"]
    if outcome == "skipped":
        report.skipped += 1
        return
    report.attempted += 1
    setattr(report, outcome, getattr(report, outcome) + 1)
    report.input_tokens += record.get("input_tokens", 0)
    report.output_tokens += record.get("output_tokens", 0)


# ---------------------------------------------------------- follow-ups --


def answer_questions(
    conn: sqlite3.Connection, application_id: int, answers: Mapping[str, str]
) -> list[NeededInput]:
    """Store answers to a parked application's questions in the answer bank.

    A key may be the question's `key` on the form or its `answer_key`; either
    way the value is kept under the answer key, so the same question on the
    next form is answered without asking. Returns what is still unanswered.
    """
    needed = store.needed_for(conn, application_id)
    if store.get_application(conn, application_id) is None:
        raise ApplyError(f"No application {application_id}.")
    by_form_key = {n.key: n for n in needed}
    by_answer_key = {n.answer_key: n for n in needed}
    for key, value in answers.items():
        value = (value or "").strip()
        if not value:
            continue
        question = by_form_key.get(key) or by_answer_key.get(key)
        if question is not None:
            set_answer(conn, question.answer_key, value, question_pattern=question.label)
        else:
            set_answer(conn, key, value)
    answered = {n.answer_key for n in needed} & set(row["key"] for row in list_answers(conn))
    return [n for n in needed if n.answer_key not in answered]


def retry_application(
    conn: sqlite3.Connection,
    application_id: int,
    settings: Settings,
    *,
    mode: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Try a parked or failed application's form again, in its own mode unless told otherwise."""
    app = store.get_application(conn, application_id)
    if app is None:
        raise ApplyError(f"No application {application_id}.")
    return apply_to_job(conn, app["job"]["id"], settings, mode=mode or app["mode"], **kwargs)


def approve_application(
    conn: sqlite3.Connection, application_id: int, settings: Settings, **kwargs: Any
) -> dict[str, Any]:
    """Submit an application that a review or dry run left at the button."""
    app = store.get_application(conn, application_id)
    if app is None:
        raise ApplyError(f"No application {application_id}.")
    if app["submitted_at"]:
        raise ApplyError("This application was already submitted.")
    return apply_to_job(conn, app["job"]["id"], settings, mode="auto", **kwargs)
