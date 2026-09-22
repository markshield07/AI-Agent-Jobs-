"""Dashboard API for response tracking: poll the mailbox, read the log, and
attach by hand a message the matcher would not place.

One poll at a time, like an apply run: a second caller gets 409 rather than two
connections to the same mailbox.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jobagent.api.routes import DbDep, SettingsDep
from jobagent.inbox import store
from jobagent.inbox.mailbox import InboxNotConfigured
from jobagent.inbox.models import LABEL_STATUS
from jobagent.inbox.poller import attach_message, poll_inbox

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

LABELS = (
    "acknowledgement",
    "screening",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "unknown",
)


class PollIn(BaseModel):
    since: str | None = Field(None, description="ISO date; defaults to the newest mail on file.")
    limit: int | None = Field(None, ge=1, le=1000)
    write: bool = True


class PollOut(BaseModel):
    started: bool
    report: dict[str, Any] | None = None


class AttachIn(BaseModel):
    application_id: int
    to_status: Literal["screening", "interviewing", "offer", "rejected", "withdrawn"] | None = None


@router.post("/inbox/poll", response_model=PollOut, status_code=202)
def poll(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    body: PollIn | None = None,
    wait: bool = False,
) -> Any:
    """Read the mailbox once. One at a time; `?wait=true` returns the report."""
    body = body or PollIn()
    lock: threading.Lock = request.app.state.inbox_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A mailbox poll is already running.")
    state = request.app.state

    def work() -> dict[str, Any] | None:
        try:
            report = poll_inbox(
                db.connection(), settings, since=body.since, limit=body.limit, write=body.write
            )
            state.last_inbox_report = report.as_dict()
            log.info(report.summary())
            return state.last_inbox_report
        except Exception:
            log.exception("mailbox poll failed")
            return None
        finally:
            lock.release()

    if wait:
        report = work()
        if report and report.get("error"):
            raise HTTPException(400, report["error"])
        return JSONResponse(
            status_code=200, content=PollOut(started=True, report=report).model_dump()
        )
    threading.Thread(target=work, name="inbox-poll", daemon=True).start()
    return PollOut(started=True)


@router.get("/inbox/last")
def last_poll(request: Request) -> dict[str, Any]:
    report = getattr(request.app.state, "last_inbox_report", None)
    if report is None:
        raise HTTPException(404, "No mailbox poll yet.")
    return report


@router.get("/inbox/counts")
def inbox_counts(db: DbDep) -> dict[str, int]:
    return store.counts(db.connection())


@router.get("/inbox")
def list_inbox(
    db: DbDep,
    application_id: int | None = None,
    matched: bool | None = None,
    label: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    if label is not None and label not in LABELS:
        raise HTTPException(400, f"Unknown label {label!r}. One of: {', '.join(LABELS)}.")
    return store.list_messages(
        db.connection(),
        application_id=application_id,
        matched=matched,
        label=label,
        limit=limit,
        offset=offset,
    )


@router.post("/inbox/{row_id}/attach")
def attach(row_id: int, body: AttachIn, db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    """File a message against an application, and move its status if asked."""
    if body.to_status is not None and body.to_status not in LABEL_STATUS:
        raise HTTPException(400, f"Unknown status {body.to_status!r}.")
    try:
        return attach_message(
            db.connection(),
            row_id,
            body.application_id,
            to_status=body.to_status,
            settings=settings,
        )
    except (ValueError, InboxNotConfigured) as exc:
        raise HTTPException(404, str(exc)) from exc
