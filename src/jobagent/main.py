"""Application entry point: the API server and the command line.

jobagent                 serve the API on 127.0.0.1:8000
jobagent serve           the same, with --host and --port
jobagent discover        run one discovery pass now and print what it did
jobagent criteria        show or change what to search for
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from jobagent.api.discovery import router as discovery_router
from jobagent.api.routes import router
from jobagent.config import Settings, get_settings
from jobagent.db.database import open_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = open_database(settings.db_path)
        app.state.discovery_lock = threading.Lock()
        app.state.last_report = None
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Job Agent", version="0.2.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(discovery_router)
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


def run(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        if args.command is None:
            args.host, args.port = "127.0.0.1", 8000
        code = _cmd_serve(args)
    elif command == "discover":
        code = _cmd_discover(args)
    else:
        code = _cmd_criteria(args)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    run()
