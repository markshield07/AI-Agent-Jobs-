"""Dashboard API for submission: start an apply pass, read applications, answer
what a form asked, approve a reviewed one, and log what happened after.

Anything that opens the browser takes the apply lock, so one form is filled
at a time; a second caller gets 409 rather than a second browser.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from jobagent.api.routes import DbDep, SettingsDep
from jobagent.apply import store
from jobagent.apply.models import MODES
from jobagent.apply.pipeline import (
    ApplyError,
    answer_questions,
    approve_application,
    retry_application,
    run_apply,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

STATUSES = (
    "pending",
    "dry_run",
    "review",
    "needs_input",
    "blocked",
    "unconfirmed",
    "failed",
    *store.EVENT_STATUSES,
)


class ApplyIn(BaseModel):
    job_ids: list[str] = Field(default_factory=list)
    limit: int = Field(10, ge=1, le=200)
    mode: Literal["dry_run", "review", "auto"] | None = None


class ApplyOut(BaseModel):
    started: bool
    report: dict[str, Any] | None = None


class AnswersIn(BaseModel):
    answers: dict[str, str]
    retry: bool = False


class EventIn(BaseModel):
    kind: Literal["status_change", "note"] = "status_change"
    to_status: Literal["applied", "screening", "interviewing", "offer", "rejected", "withdrawn"] | (
        None
    ) = None
    note: str | None = None


@contextmanager
def _browser_slot(request: Request):
    lock: threading.Lock = request.app.state.apply_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "An apply run is already using the browser.")
    try:
        yield
    finally:
        lock.release()


# ------------------------------------------------------------------ runs --


@router.post("/apply", response_model=ApplyOut, status_code=202)
def apply(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    body: ApplyIn | None = None,
    wait: bool = False,
) -> Any:
    """Start an apply pass. One at a time; `?wait=true` returns the report."""
    body = body or ApplyIn()
    if body.mode is not None and body.mode not in MODES:
        raise HTTPException(400, f"Unknown mode {body.mode!r}.")
    lock: threading.Lock = request.app.state.apply_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "An apply run is already using the browser.")
    state = request.app.state

    def work() -> dict[str, Any] | None:
        try:
            report = run_apply(
                db.connection(),
                settings,
                job_ids=body.job_ids or None,
                limit=body.limit,
                mode=body.mode,
            )
            state.last_apply_report = report.as_dict()
            log.info(report.summary())
            return state.last_apply_report
        except Exception:
            log.exception("apply run failed")
            return None
        finally:
            lock.release()

    if wait:
        report = work()
        return JSONResponse(
            status_code=200, content=ApplyOut(started=True, report=report).model_dump()
        )
    threading.Thread(target=work, name="apply", daemon=True).start()
    return ApplyOut(started=True)


@router.get("/apply/last")
def last_apply(request: Request) -> dict[str, Any]:
    report = getattr(request.app.state, "last_apply_report", None)
    if report is None:
        raise HTTPException(404, "No apply run yet.")
    return report


# ---------------------------------------------------------- applications --


@router.get("/applications")
def get_applications(
    db: DbDep,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"Unknown status {status!r}. One of: {', '.join(STATUSES)}.")
    return store.list_applications(db.connection(), status=status, limit=limit, offset=offset)


@router.get("/applications/counts")
def get_application_counts(db: DbDep) -> dict[str, int]:
    return store.count_applications(db.connection())


def _get(db, application_id: int) -> dict[str, Any]:
    app = store.get_application(db.connection(), application_id)
    if app is None:
        raise HTTPException(404, f"No application {application_id}.")
    return app


@router.get("/applications/{application_id}")
def get_application(application_id: int, db: DbDep) -> dict[str, Any]:
    app = _get(db, application_id)
    conn = db.connection()
    app["attempts"] = store.list_attempts(conn, application_id)
    app["events"] = store.list_events(conn, application_id)
    return app


@router.get("/applications/{application_id}/screenshot")
def get_screenshot(application_id: int, db: DbDep) -> FileResponse:
    app = _get(db, application_id)
    path = app.get("screenshot_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "This application has no screenshot.")
    return FileResponse(path, media_type="image/png", filename=Path(path).name)


@router.post("/applications/{application_id}/answers")
def post_answers(
    application_id: int, body: AnswersIn, request: Request, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """Answer what the form asked. With `retry`, fill the form again right away."""
    _get(db, application_id)
    conn = db.connection()
    remaining = answer_questions(conn, application_id, body.answers)
    result = None
    if body.retry:
        with _browser_slot(request):
            try:
                result = retry_application(conn, application_id, settings)
            except ApplyError as exc:
                raise HTTPException(409, str(exc)) from exc
    return {
        "application": store.get_application(conn, application_id),
        "needed": [n.as_dict() for n in remaining],
        "result": result,
    }


@router.post("/applications/{application_id}/approve")
def post_approve(
    application_id: int, request: Request, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """Submit an application that a dry run or review left at the button."""
    _get(db, application_id)
    with _browser_slot(request):
        try:
            result = approve_application(db.connection(), application_id, settings)
        except ApplyError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"application": store.get_application(db.connection(), application_id), "result": result}


@router.post("/applications/{application_id}/events", status_code=201)
def post_event(application_id: int, body: EventIn, db: DbDep) -> list[dict[str, Any]]:
    """Log what happened after: a reply, an interview, a rejection, a note."""
    _get(db, application_id)
    conn = db.connection()
    try:
        store.add_event(
            conn,
            application_id,
            kind=body.kind,
            to_status=body.to_status,
            note=body.note,
            source="manual",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return store.list_events(conn, application_id)
