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
jobagent login           sign in to LinkedIn or Indeed once, in a visible browser
jobagent run             one full cycle: discover, tailor, apply, read replies
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path

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
        help="Sign in to LinkedIn or Indeed once, in a visible browser; the password is not kept.",
    )
    login.add_argument("site", choices=("linkedin", "indeed"))
    login.add_argument("--status", action="store_true", help="Show whether a sign-in is saved.")
    login.add_argument("--forget", action="store_true", help="Delete the saved sign-in.")
    login.add_argument(
        "--timeout", type=int, default=300, help="Seconds to wait for you to sign in."
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
            timeout_s=args.timeout,
        )
        path = sessions.save_session(settings, site.name, state)
    except (BrowserUnavailable, TimeoutError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Signed in to {site.label}. Saved to {path}.")
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
