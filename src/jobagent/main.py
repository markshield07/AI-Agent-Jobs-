"""Application entry point: the API server and the command line.

jobagent                 serve the API on 127.0.0.1:8000
jobagent serve           the same, with --host and --port
jobagent discover        run one discovery pass now and print what it did
jobagent criteria        show or change what to search for
jobagent tailor          tailor the resume to jobs and render PDFs
jobagent apply           fill application forms; submit only in auto mode
jobagent applications    list applications and what they wait on
jobagent answer          answer questions a form asked, then try again
jobagent approve         submit an application left at the button
jobagent inbox           read replies from the mailbox and record what they say
jobagent login           sign in to LinkedIn, Indeed or a company's Workday, once
jobagent run             one full cycle: discover, tailor, apply, read replies
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jobagent.api.applications import router as applications_router
from jobagent.api.discovery import router as discovery_router
from jobagent.api.inbox import router as inbox_router
from jobagent.api.routes import router
from jobagent.api.sessions import router as sessions_router
from jobagent.api.stats import router as stats_router
from jobagent.api.tailoring import router as tailoring_router
from jobagent.config import Settings, get_settings
from jobagent.db.database import open_database

DASHBOARD_DIR = Path(__file__).parent / "dashboard"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = open_database(settings.db_path)
        app.state.discovery_lock = threading.Lock()
        app.state.last_report = None
        app.state.apply_lock = threading.Lock()
        app.state.last_apply_report = None
        app.state.inbox_lock = threading.Lock()
        app.state.last_inbox_report = None
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Job Agent", version="0.6.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(tailoring_router)
    app.include_router(applications_router)
    app.include_router(inbox_router)
    app.include_router(sessions_router)
    app.include_router(stats_router)
    # The dashboard: plain files, no build step, served from the same origin as
    # the API so it needs no CORS and no configuration.
    app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app


# ------------------------------------------------------------------- CLI --


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobagent", description="Find, score and apply to jobs.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the API and dashboard.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    discover = sub.add_parser("discover", help="Run one discovery pass now.")
    discover.add_argument("--no-enrich", action="store_true", help="Skip fetching full postings.")
    discover.add_argument("--no-classify", action="store_true", help="Rules only, no model call.")
    discover.add_argument(
        "--enrich-with-model",
        action="store_true",
        help="Let the model extract a description when the page's markup gives nothing.",
    )
    discover.add_argument("--json", action="store_true", help="Print the full report as JSON.")

    criteria = sub.add_parser("criteria", help="Show or change what to search for.")
    criteria.add_argument("--title", action="append", help="A job title to search for.")
    criteria.add_argument("--location", action="append", help="A location, or Remote.")
    criteria.add_argument("--keyword", action="append", help="A term that earns a posting points.")
    criteria.add_argument("--exclude", action="append", help="A term that rejects a posting.")
    criteria.add_argument(
        "--board",
        action="append",
        metavar="ATS:SLUG[:COMPANY]",
        help="A company ATS board, e.g. greenhouse:stripe or lever:netflix:Netflix.",
    )
    criteria.add_argument("--site", action="append", help="A JobSpy site: indeed, linkedin, ...")
    criteria.add_argument("--salary-min", type=int)
    criteria.add_argument("--min-score", type=int)
    criteria.add_argument("--max-tier", type=int)
    criteria.add_argument("--max-age-hours", type=int)
    criteria.add_argument(
        "--no-remote", action="store_true", help="Do not count remote as a match."
    )

    tailor = sub.add_parser("tailor", help="Tailor the resume to jobs and render PDFs.")
    tailor.add_argument("job_ids", nargs="*", help="Job ids to tailor for.")
    tailor.add_argument("--queued", action="store_true", help="Every queued job without a variant.")
    tailor.add_argument("--limit", type=int, default=10, help="With --queued: at most this many.")
    tailor.add_argument("--no-cover-letter", action="store_true")

    apply = sub.add_parser("apply", help="Fill application forms; submit only in auto mode.")
    apply.add_argument("job_ids", nargs="*", help="Job ids to apply to.")
    apply.add_argument("--queued", action="store_true", help="Every queued job with a resume.")
    apply.add_argument("--limit", type=int, default=10, help="With --queued: at most this many.")
    apply.add_argument(
        "--mode",
        choices=("dry_run", "review", "auto"),
        help="dry_run fills and stops; review parks for approval; auto submits.",
    )
    apply.add_argument("--submit", action="store_true", help="The same as --mode auto.")
    apply.add_argument("--headed", action="store_true", help="Show the browser window.")
    apply.add_argument(
        "--no-generic", action="store_true", help="Skip forms no handler recognises."
    )
    apply.add_argument("--json", action="store_true", help="Print the full report as JSON.")

    applications = sub.add_parser("applications", help="List applications and what they wait on.")
    applications.add_argument("--status", help="Only this status, e.g. needs_input or applied.")
    applications.add_argument("--json", action="store_true")

    answer = sub.add_parser("answer", help="Answer questions a form asked, then try again.")
    answer.add_argument("application_id", type=int)
    answer.add_argument("pairs", nargs="+", metavar="KEY=VALUE", help="Answers, by key.")
    answer.add_argument("--no-retry", action="store_true", help="Store the answers only.")
    answer.add_argument("--headed", action="store_true")

    approve = sub.add_parser("approve", help="Submit an application left at the button.")
    approve.add_argument("application_id", type=int)
    approve.add_argument("--headed", action="store_true")

    inbox = sub.add_parser("inbox", help="Read replies and record what they say.")
    inbox.add_argument("--list", action="store_true", help="Show the log instead of polling.")
    inbox.add_argument("--unmatched", action="store_true", help="With --list: only unattached.")
    inbox.add_argument(
        "--attach",
        metavar="MESSAGE=APPLICATION",
        help="File a message against an application, e.g. 12=3.",
    )
    inbox.add_argument("--status", help="With --attach: also move the application to this status.")
    inbox.add_argument("--since", help="Read mail received on or after this date, e.g. 2026-09-01.")
    inbox.add_argument("--limit", type=int, help="At most this many messages.")
    inbox.add_argument(
        "--dry-run", action="store_true", help="Read and classify, but record nothing."
    )
    inbox.add_argument("--json", action="store_true", help="Print the full report as JSON.")

    login = sub.add_parser(
        "login",
        help="Sign in to LinkedIn, Indeed or a company's Workday once, in a visible browser; "
        "the password is not kept.",
    )
    login.add_argument("site", choices=("linkedin", "indeed", "workday"))
    login.add_argument(
        "url",
        nargs="?",
        help="Workday only: a posting on the company's Workday site "
        "(https://<company>.wd5.myworkdayjobs.com/...). Each company has its own account.",
    )
    login.add_argument(
        "--status",
        action="store_true",
        help="Show whether a sign-in is saved. For Workday it also opens each company's "
        "site to see whether the sign-in still works (skip that with --no-check).",
    )
    login.add_argument(
        "--no-check", action="store_true", help="Workday --status: don't open the sites."
    )
    login.add_argument(
        "--check-timeout",
        type=int,
        default=90,
        help="Workday --status: seconds each site may take to show whether it is signed in.",
    )
    login.add_argument(
        "--no-retry",
        action="store_true",
        help="Workday: after signing in, don't run the applications waiting on it.",
    )
    login.add_argument(
        "--keep-alive",
        action="store_true",
        help="Workday: visit each company's Candidate Home with its saved sign-in every "
        "--every minutes, to keep the sign-in from ending; runs until stopped.",
    )
    login.add_argument(
        "--every", type=float, default=15, help="--keep-alive: minutes between visits (15)."
    )
    login.add_argument("--once", action="store_true", help="--keep-alive: one round, then stop.")
    login.add_argument("--forget", action="store_true", help="Delete the saved sign-in.")
    login.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Seconds to wait for you to sign in (5 minutes; 15 for Workday, whose new "
        "accounts need an email check).",
    )

    cycle = sub.add_parser(
        "run", help="One full cycle: find jobs, tailor, apply (in the set mode), read replies."
    )
    cycle.add_argument(
        "--every", type=float, metavar="HOURS", help="Keep running, a cycle every this many hours."
    )
    cycle.add_argument("--tailor-limit", type=int, default=10)
    cycle.add_argument("--apply-limit", type=int, default=10)
    cycle.add_argument(
        "--skip", action="append", choices=("discover", "tailor", "apply", "inbox"), default=[]
    )
    cycle.add_argument("--json", action="store_true", help="Print each cycle's report as JSON.")
    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("jobagent.main:create_app", factory=True, host=args.host, port=args.port)
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    from jobagent.discovery.pipeline import run_discovery

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    db = open_database(settings.db_path)
    try:
        report = run_discovery(
            db.connection(),
            settings,
            enrich=not args.no_enrich,
            enrich_with_model=args.enrich_with_model,
            classify=not args.no_classify,
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0
    print(report.summary())
    for name, error in report.source_errors.items():
        print(f"  {name} failed: {error}")
    for note in report.notes:
        print(f"  note: {note}")
    return 0


def _parse_board(text: str):
    from jobagent.discovery.criteria import BoardRef

    parts = text.split(":", 2)
    if len(parts) < 2 or not parts[1]:
        raise SystemExit(f"--board wants ATS:SLUG[:COMPANY], got {text!r}")
    ats, slug = parts[0].strip().lower(), parts[1].strip()
    company = parts[2].strip() if len(parts) == 3 else None
    return BoardRef(ats=ats, slug=slug, company=company)


def _cmd_criteria(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from jobagent.discovery.criteria import dump_criteria, load_criteria, save_criteria

    settings = get_settings()
    db = open_database(settings.db_path)
    try:
        conn = db.connection()
        current = load_criteria(conn)
        changes: dict[str, object] = {}
        if args.title:
            changes["titles"] = args.title
        if args.location:
            changes["locations"] = args.location
        if args.keyword:
            changes["keywords"] = args.keyword
        if args.exclude:
            changes["exclude_keywords"] = args.exclude
        if args.board:
            changes["boards"] = [_parse_board(b) for b in args.board]
        if args.site:
            changes["jobspy_sites"] = [s.strip().lower() for s in args.site]
        for name in ("salary_min", "min_score", "max_tier", "max_age_hours"):
            value = getattr(args, name)
            if value is not None:
                changes[name] = value
        if args.no_remote:
            changes["remote_ok"] = False

        if changes:
            try:
                updated = current.model_validate({**current.model_dump(), **changes})
            except ValidationError as exc:
                print(exc, file=sys.stderr)
                return 2
            save_criteria(conn, updated)
            current = updated
        print(json.dumps(dump_criteria(current), indent=2))
    finally:
        db.close()
    return 0


def _cmd_tailor(args: argparse.Namespace) -> int:
    from jobagent.discovery import store as jobs
    from jobagent.llm.backend import LLMError
    from jobagent.tailor import store as variants
    from jobagent.tailor.pipeline import TailorError, tailor_job

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    db = open_database(settings.db_path)
    failures = 0
    try:
        conn = db.connection()
        ids = list(args.job_ids)
        if args.queued:
            for job in jobs.list_jobs(conn, status="queued", limit=500):
                if variants.latest_ready_variant(conn, job["id"]) is None:
                    ids.append(job["id"])
                if len(ids) >= args.limit:
                    break
        if not ids:
            print("Nothing to tailor: pass job ids or use --queued.", file=sys.stderr)
            return 2
        for jid in ids:
            try:
                variant = tailor_job(
                    conn, jid, settings, with_cover_letter=not args.no_cover_letter
                )
            except (TailorError, LLMError) as exc:
                failures += 1
                print(f"{jid}: {exc}")
                continue
            print(_describe_variant(jid, variant))
    finally:
        db.close()
    return 1 if failures else 0


def _describe_variant(jid: str, variant) -> str:
    cov = f"{variant.keyword_coverage:.0%}" if variant.keyword_coverage is not None else "n/a"
    base = f"{variant.base_coverage:.0%}" if variant.base_coverage is not None else "n/a"
    if variant.status == "ready":
        letter = "with cover letter" if variant.cover_letter else "no cover letter"
        line = (
            f"{jid}: ready, keywords {cov} (uploaded resume {base}), {letter}, {variant.pdf_path}"
        )
    else:
        line = f"{jid}: rejected after {variant.attempts} attempts"
    for issue in variant.issues[:6]:
        line += f"\n    {issue.where}: {issue.message}" + (f" [{issue.term}]" if issue.term else "")
    return line


def _settings_for(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if getattr(args, "headed", False):
        settings = settings.model_copy(update={"headless": False})
    return settings


def _describe_result(record: dict) -> str:
    outcome = record["outcome"]
    line = f"{record['job_id']}: {outcome}"
    if record.get("handler"):
        line += f" via {record['handler']}"
    if outcome == "skipped":
        line += f" ({record.get('reason')})"
    elif record.get("confirmation"):
        line += f": {record['confirmation']}"
    elif record.get("error"):
        line += f": {record['error']}"
    elif outcome in ("dry_run", "review"):
        line += f", {record.get('filled', 0)} fields filled, application {record['application_id']}"
        if record.get("screenshot_path"):
            line += f", {record['screenshot_path']}"
    for question in record.get("needed") or []:
        if question.get("required"):
            line += f"\n    ask: {question['label']} [{question['answer_key']}]"
    return line


def _cmd_apply(args: argparse.Namespace) -> int:
    from jobagent.apply.pipeline import ApplyError, run_apply

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not args.job_ids and not args.queued:
        print("Nothing to apply to: pass job ids or use --queued.", file=sys.stderr)
        return 2
    settings = _settings_for(args)
    mode = "auto" if args.submit else args.mode
    db = open_database(settings.db_path)
    try:
        report = run_apply(
            db.connection(),
            settings,
            job_ids=args.job_ids or None,
            limit=args.limit,
            mode=mode,
            allow_generic=not args.no_generic,
        )
    except ApplyError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        db.close()
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.summary())
        for record in report.results:
            print(_describe_result(record))
        for note in report.notes:
            print(f"  note: {note}")
    return 1 if report.failed else 0


def _cmd_applications(args: argparse.Namespace) -> int:
    from jobagent.apply import store as applications

    settings = get_settings()
    db = open_database(settings.db_path)
    try:
        rows = applications.list_applications(db.connection(), status=args.status, limit=500)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("No applications yet.")
        return 0
    for app in rows:
        job = app["job"]
        line = f"{app['id']}: {app['status']}  {job['title']} at {job['company']}"
        if app["submitted_at"]:
            line += f"  submitted {app['submitted_at']}"
        print(line)
        for question in app["needed"]:
            if question.get("required"):
                print(f"    ask: {question['label']} [{question['answer_key']}]")
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from jobagent.apply.pipeline import ApplyError, answer_questions, retry_application

    answers: dict[str, str] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            print(f"Expected KEY=VALUE, got {pair!r}.", file=sys.stderr)
            return 2
        answers[key.strip()] = value.strip()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = _settings_for(args)
    db = open_database(settings.db_path)
    try:
        conn = db.connection()
        try:
            remaining = answer_questions(conn, args.application_id, answers)
            for question in remaining:
                if question.required:
                    print(f"still needed: {question.label} [{question.answer_key}]")
            if args.no_retry:
                return 0
            record = retry_application(conn, args.application_id, settings)
        except ApplyError as exc:
            print(exc, file=sys.stderr)
            return 2
    finally:
        db.close()
    print(_describe_result(record))
    return 1 if record["outcome"] == "failed" else 0


def _cmd_approve(args: argparse.Namespace) -> int:
    from jobagent.apply.pipeline import ApplyError, approve_application

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = _settings_for(args)
    db = open_database(settings.db_path)
    try:
        try:
            record = approve_application(db.connection(), args.application_id, settings)
        except ApplyError as exc:
            print(exc, file=sys.stderr)
            return 2
    finally:
        db.close()
    print(_describe_result(record))
    return 1 if record["outcome"] == "failed" else 0


def _describe_reply(result: dict) -> str:
    message = result["message"]
    who = message["from_name"] or message["from_addr"]
    line = f"{message['received_at']}  {who}: {message['subject']}"
    match, reading = result.get("match"), result.get("reading")
    if match is None:
        return line + "\n    unmatched, attach it with: jobagent inbox --attach ID=APPLICATION"
    line += f"\n    application {match['application_id']} ({match['reason']})"
    if reading:
        line += f"\n    reads as {reading['label']} ({reading['reason']})"
        if result.get("advanced"):
            line += f", moved to {reading['status']}"
        elif reading["status"]:
            line += ", status unchanged"
    return line


def _cmd_inbox(args: argparse.Namespace) -> int:
    from jobagent.inbox import store as inbox
    from jobagent.inbox.poller import attach_message, poll_inbox

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    db = open_database(settings.db_path)
    try:
        conn = db.connection()
        if args.attach:
            row_id, sep, application_id = args.attach.partition("=")
            if not sep or not row_id.strip().isdigit() or not application_id.strip().isdigit():
                print("Expected MESSAGE=APPLICATION, both numbers.", file=sys.stderr)
                return 2
            try:
                done = attach_message(
                    conn,
                    int(row_id),
                    int(application_id),
                    to_status=args.status,
                    settings=settings,
                )
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            where = done["application"]
            print(f"message {row_id} filed against application {where['id']}, {where['status']}")
            return 0

        if args.list:
            rows = inbox.list_messages(
                conn, matched=False if args.unmatched else None, limit=args.limit or 100
            )
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
                return 0
            if not rows:
                print("No messages read yet.")
                return 0
            for row in rows:
                where = f"application {row['application_id']}" if row["application_id"] else "-"
                print(f"{row['id']}: {row['received_at']}  {row['label']:15} {where}")
                print(f"    {row['from_addr']}: {row['subject']}")
            return 0

        report = poll_inbox(
            conn, settings, since=args.since, limit=args.limit, write=not args.dry_run
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 1 if report.error else 0
    if report.error:
        print(report.error, file=sys.stderr)
        return 2
    print(report.summary() + (" Nothing was recorded." if args.dry_run else ""))
    for result in report.results:
        print(_describe_reply(result))
    for note in report.notes:
        print(f"  note: {note}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from jobagent.apply import sessions
    from jobagent.apply.browser.session import BrowserUnavailable, interactive_login

    settings = get_settings()
    if args.site == "workday":
        return _login_workday(args, settings)
    site = sessions.site(args.site)
    if args.forget:
        gone = sessions.forget_session(settings, site.name)
        print(f"{site.label} sign-in deleted." if gone else f"No {site.label} sign-in was saved.")
        return 0
    if args.status:
        status = sessions.session_status(settings, site.name)
        if not status["saved"]:
            print(f"{site.label}: not signed in. Run: jobagent login {site.name}")
        elif not status["signed_in"]:
            print(f"{site.label}: the saved sign-in has expired. Run: jobagent login {site.name}")
        else:
            until = f", good until {status['expires_at']}" if status["expires_at"] else ""
            print(f"{site.label}: signed in (saved {status['saved_at']}{until}).")
        return 0

    print(
        f"A browser window will open at {site.label}'s sign-in page. Sign in there as you "
        "normally would, including any code the site asks for. The password goes to "
        f"{site.label} only; what is kept is the site's cookies, in "
        f"{sessions.session_path(settings, site.name)} (readable by you only)."
    )
    try:
        state = interactive_login(
            settings,
            site.login_url,
            lambda cookies: sessions.signed_in(
                sessions.site_cookies({"cookies": cookies}, site.name), site.name
            ),
            timeout_s=args.timeout or 300,
        )
        path = sessions.save_session(settings, site.name, state)
    except (BrowserUnavailable, TimeoutError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Signed in to {site.label}. Saved to {path}.")
    return 0


def _login_workday(args: argparse.Namespace, settings: Settings) -> int:
    from jobagent.apply import sessions
    from jobagent.apply.browser.session import BrowserUnavailable, interactive_login

    if args.keep_alive:
        return _keep_workday_alive(settings, every_min=args.every, once=args.once)
    if args.status and not args.url:
        hosts = sessions.workday_hosts(settings)
        if not hosts:
            print("Workday: no company sign-ins saved. Run: jobagent login workday <posting URL>")
        if hosts and not args.no_check:
            _check_workday(settings, hosts, timeout_s=args.check_timeout)
        for host in hosts:
            print(_workday_line(sessions.workday_status(settings, host)))
        return 0
    if not args.url:
        print(
            "Give the posting's link: jobagent login workday "
            "https://<company>.wd5.myworkdayjobs.com/...",
            file=sys.stderr,
        )
        return 2
    try:
        host = sessions.workday_host(args.url)
    except sessions.UnknownSite as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.forget:
        gone = sessions.forget_workday_session(settings, host)
        print(f"Workday sign-in for {host} deleted." if gone else f"No sign-in saved for {host}.")
        return 0
    if args.status:
        if not args.no_check and sessions.workday_status(settings, host)["saved"]:
            _check_workday(settings, [host], url=args.url, timeout_s=args.check_timeout)
        print(_workday_line(sessions.workday_status(settings, host)))
        return 0

    print(
        f"A browser window will open at {host}. Each company on Workday has its own account: "
        "use Sign In at the top of the page, or Create Account if you have never applied "
        "there, and finish any email check it asks for. The password goes to Workday only; "
        f"what is kept is this company's cookies, in "
        f"{sessions.workday_session_path(settings, host)} (readable by you only).\n"
        "When you are signed in, come back here and press Enter."
    )
    pressed = threading.Event()

    def wait_for_enter() -> None:
        try:
            sys.stdin.readline()
        except (OSError, ValueError):
            return
        pressed.set()

    threading.Thread(target=wait_for_enter, daemon=True).start()
    try:
        state = interactive_login(
            settings,
            args.url,
            lambda cookies: False,
            timeout_s=args.timeout or 900,
            confirmed=pressed.is_set,
        )
        path = sessions.save_workday_session(settings, host, state, url=args.url)
    except (BrowserUnavailable, TimeoutError, ValueError) as exc:
        print(
            f"NOT SAVED: nothing was kept for {host} ({exc}). Run the same command again, "
            "sign in, and press Enter here once the page shows you signed in.",
            file=sys.stderr,
        )
        return 2
    print(f"Saved the Workday sign-in for {host} to {path}.")
    if not args.no_check:
        # Enter pressed before the sign-in finished saves cookies that do not work.
        print("Checking that the sign-in works...")
        checked = _check_workday(settings, [host], url=args.url, timeout_s=args.check_timeout)
        if checked.get(host) == "signed_out":
            print(
                f"NOT SIGNED IN: {host} still shows its sign-in page, so the sign-in did not "
                "finish. Run the same command again and press Enter only once the page shows "
                "you signed in.",
                file=sys.stderr,
            )
            return 2
        if checked.get(host) == "signed_in":
            print(f"Signed in to {host}: it works.")
    waiting = sessions.workday_waiting(settings, host)
    if waiting and args.no_retry:
        print(
            f"{len(waiting)} application(s) wait on this sign-in; run them with: jobagent apply "
            + " ".join(waiting)
        )
    elif waiting:
        # Straight away: the sign-in only lasts so long (about an hour on CrowdStrike's).
        print(
            f"Running the {len(waiting)} application(s) that were waiting on it now. "
            "Leave this window open until they finish."
        )
        return _run_waiting(settings, waiting, host=host)
    return 0


def _workday_line(status: dict[str, object]) -> str:
    host = status["host"]
    state = status["state"]
    if state == "not_signed_in":
        text = f"not signed in. Run: {status['login_command']}"
    elif state == "needs_sign_in":
        text = f"the sign-in has ended; sign in again: {status['login_command']}"
    elif state == "signed_in":
        text = f"signed in (saved {status['saved_at']}, working at {status['checked_at']})"
    else:
        text = f"saved {status['saved_at']}, not checked since"
    if status.get("kept_alive_at"):
        text += f"; keep-alive last visited {status['kept_alive_at']} ({status['keep_alive']})"
    if status.get("waiting"):
        text += f"; {status['waiting']} application(s) waiting on it"
    return f"Workday {host}: {text}."


def _check_workday(
    settings: Settings, hosts: list[str], url: str | None = None, *, timeout_s: float = 90
) -> dict[str, str]:
    """Open each company's posting with the saved sign-in and record what shows;
    return what each host showed."""
    from jobagent.apply import sessions
    from jobagent.apply.browser.session import BrowserUnavailable, open_browser
    from jobagent.apply.handlers.workday import WorkdayHandler

    found: dict[str, str] = {}
    try:
        with open_browser(settings) as browser:
            for host in hosts:
                where = url or sessions.workday_status(settings, host)["url"]
                if not where:
                    print(f"Workday {host}: no posting on file to check it with.")
                    continue
                handler = WorkdayHandler()
                began = time.monotonic()
                with browser.new_page() as page:
                    state = handler.check_session(page, where, timeout_s=timeout_s)
                took = time.monotonic() - began
                found[host] = state
                stages = ", ".join(f"{name} {secs:.0f}s" for name, secs in handler.timings)
                if state == "timed_out":
                    print(
                        f"Workday {host}: {where} showed neither a step nor a sign-in page "
                        f"within {timeout_s:.0f}s ({stages or 'the posting did not open'}); "
                        "not recorded. Sign in again if its applications stop."
                    )
                    continue
                if took > 30:
                    print(f"Workday {host}: the check took {took:.0f}s ({stages}).")
                if state == "unknown":
                    print(f"Workday {host}: could not tell from {where} (closed posting?).")
                    continue
                sessions.record_workday_check(settings, host, state)
    except BrowserUnavailable as exc:
        print(f"Could not open a browser to check: {exc}", file=sys.stderr)
    return found


def _keep_workday_alive(settings: Settings, *, every_min: float, once: bool) -> int:
    """Visit each signed-in company's Candidate Home now and every `every_min`
    minutes, keeping the cookies each visit leaves; a company whose sign-in
    has ended is marked for a new one and left alone until it gets it.

    Each visit is stamped into the company's sign-in file (kept_alive_at), and
    each line printed goes out at once, so a log written under nohup shows
    every round as it happens."""
    from jobagent.apply import sessions
    from jobagent.apply.browser.session import BrowserUnavailable, open_browser

    if every_min < 5:
        print("--every must be at least 5 minutes.", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        # Written to a file, print() output waits in a buffer until exit.
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    rounds = 0
    while True:
        rounds += 1
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        hosts = [
            h
            for h in sessions.workday_hosts(settings)
            if sessions.workday_status(settings, h)["saved"]
        ]
        print(
            f"{stamp} Workday keep-alive round {rounds}: "
            f"{len(hosts)} compan{'y' if len(hosts) == 1 else 'ies'} with a saved sign-in."
        )
        try:
            with open_browser(settings) as browser:
                for host in hosts:
                    _keep_one_alive(settings, browser, host, stamp)
        except BrowserUnavailable as exc:
            print(f"{stamp} Could not open a browser: {exc}", file=sys.stderr)
        except Exception as exc:  # the loop outlives any one bad round
            print(f"{stamp} Keep-alive round failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if once:
            return 0
        print(f"{stamp} Next round in {every_min:g} minutes.")
        time.sleep(every_min * 60)


def _keep_one_alive(settings: Settings, browser: Any, host: str, stamp: str) -> None:
    """One company's visit: Candidate Home, and when that page cannot be read,
    the posting on file with Apply pressed, as `login workday --status` does."""
    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    status = sessions.workday_status(settings, host)
    if status["state"] == "needs_sign_in":
        print(f"{stamp} Workday {host}: needs a new sign-in: {status['login_command']}")
        return
    if not status["url"]:
        print(f"{stamp} Workday {host}: no posting on file to find its site by.")
        return
    began = time.monotonic()
    how, landed, capture = "candidate home", "", None
    try:
        with browser.new_page() as page:
            state = WorkdayHandler().keep_alive(page, status["url"])
            landed = str(getattr(page, "url", "") or "")
            if state in ("unknown", "timed_out"):
                capture = _page_html(page)
                how = "candidate home, then the posting"
                state = WorkdayHandler().check_session(page, status["url"])
                landed = str(getattr(page, "url", "") or "")
            kept = state == "signed_in" and sessions.refresh_workday_session(
                settings, host, {"cookies": page.context.cookies()}
            )
    except Exception as exc:  # one company's failure leaves the others' visits alone
        sessions.record_keep_alive(settings, host, "error", how=how, landed=landed)
        print(f"{stamp} Workday {host}: the visit failed: {type(exc).__name__}: {exc}")
        return
    sessions.record_keep_alive(settings, host, state, how=how, landed=landed)
    took = f" ({how}, {time.monotonic() - began:.0f}s)"
    if state == "signed_out":
        print(
            f"{stamp} Workday {host}: the sign-in has ended{took}; sign in again: "
            f"{status['login_command']}"
        )
    elif kept:
        print(f"{stamp} Workday {host}: still signed in; cookies refreshed{took}.")
    else:
        print(f"{stamp} Workday {host}: could not tell ({state}){took}; trying again next round.")
    if capture and state != "signed_in":
        path = sessions.save_workday_capture(settings, host, "home", capture)
        print(f"{stamp} Workday {host}: saved what Candidate Home showed to {path}.")


def _page_html(page: Any) -> str | None:
    try:
        return page.content()
    except Exception:
        return None


def _run_waiting(settings: Settings, job_ids: list[str], *, host: str | None = None) -> int:
    from jobagent.apply import sessions
    from jobagent.apply.pipeline import ApplyError, run_apply

    db = open_database(settings.db_path)
    try:
        report = run_apply(db.connection(), settings, job_ids=job_ids, limit=len(job_ids))
    except ApplyError as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        db.close()
    if host:
        # Waiting no longer: every job that did not stop at the sign-in page again.
        done = [str(r["job_id"]) for r in report.results if not r.get("sign_in")]
        sessions.clear_workday_waiting(settings, host, done)
    print(report.summary())
    for record in report.results:
        print(_describe_result(record))
    for note in report.notes:
        print(f"  note: {note}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from jobagent.cycle import run_cycle, run_forever

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if args.every is not None and args.every < 0.25:
        print("--every must be at least 0.25 hours.", file=sys.stderr)
        return 2
    last = {"ok": True}

    def once():
        db = open_database(settings.db_path)
        try:
            report = run_cycle(
                db.connection(),
                settings,
                skip=tuple(args.skip),
                tailor_limit=args.tailor_limit,
                apply_limit=args.apply_limit,
            )
        finally:
            db.close()
        last["ok"] = report.ok
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, default=str))
        else:
            print(f"cycle at {report.started_at}, mode {settings.apply_mode}")
            for step in report.steps:
                print(f"  {step.step}: {'' if step.ok else 'FAILED '}{step.summary}")
        return report

    if args.every is None:
        once()
        return 0 if last["ok"] else 1
    print(f"Running a cycle every {args.every:g} hours. Ctrl-C stops it.")
    try:
        run_forever(once, args.every)
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


_COMMANDS = {
    "discover": _cmd_discover,
    "criteria": _cmd_criteria,
    "tailor": _cmd_tailor,
    "apply": _cmd_apply,
    "applications": _cmd_applications,
    "answer": _cmd_answer,
    "approve": _cmd_approve,
    "inbox": _cmd_inbox,
    "login": _cmd_login,
    "run": _cmd_run,
}


def run(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        if args.command is None:
            args.host, args.port = "127.0.0.1", 8000
        code = _cmd_serve(args)
    else:
        code = _COMMANDS[command](args)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    run()
