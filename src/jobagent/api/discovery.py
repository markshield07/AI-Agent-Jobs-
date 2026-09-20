"""Dashboard API for discovery: what to search for, what was found, and runs.

Same shape as the phase 1 routes: sync handlers, a connection resolved in the
handler body. A discovery run is started in a thread of its own so the request
returns at once; `?wait=true` runs it inline and returns the full report.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from jobagent.api.models import DiscoverOut, JobStatusIn
from jobagent.api.routes import DbDep, SettingsDep
from jobagent.discovery import store
from jobagent.discovery.criteria import SearchCriteria, dump_criteria, load_criteria, save_criteria
from jobagent.discovery.pipeline import run_discovery

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ------------------------------------------------------------ criteria --


@router.get("/search-criteria")
def get_search_criteria(db: DbDep) -> dict[str, Any]:
    return dump_criteria(load_criteria(db.connection()))


@router.put("/search-criteria")
def put_search_criteria(body: SearchCriteria, db: DbDep) -> dict[str, Any]:
    """Replace the criteria wholesale. Send the full document back, edited."""
    save_criteria(db.connection(), body)
    return dump_criteria(body)


# ----------------------------------------------------------------- jobs --


@router.get("/jobs")
def get_jobs(
    db: DbDep,
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    if status is not None and status not in store.STATUSES:
        raise HTTPException(400, f"Unknown status {status!r}. One of: {', '.join(store.STATUSES)}.")
    return store.list_jobs(
        db.connection(),
        status=status,
        min_score=min_score,
        source=source,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/counts")
def get_job_counts(db: DbDep) -> dict[str, int]:
    return store.count_jobs(db.connection())


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: DbDep) -> dict[str, Any]:
    job = store.get_job(db.connection(), job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}.")
    return job


@router.patch("/jobs/{job_id}")
def update_job(job_id: str, body: JobStatusIn, db: DbDep) -> dict[str, Any]:
    """Queue a posting the scorer skipped, or skip one it queued."""
    conn = db.connection()
    if not store.set_status(conn, job_id, body.status):
        raise HTTPException(404, f"No job {job_id}.")
    job = store.get_job(conn, job_id)
    assert job is not None
    return job


# ----------------------------------------------------------------- runs --


@router.get("/runs")
def get_runs(db: DbDep, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    return store.list_runs(db.connection(), limit=limit)


@router.get("/runs/{run_id}")
def get_run(run_id: int, request: Request, db: DbDep) -> dict[str, Any]:
    found = db.connection().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if found is None:
        raise HTTPException(404, f"No run {run_id}.")
    row = dict(found)
    report = getattr(request.app.state, "last_report", None)
    if report and report.get("run_id") == run_id:
        row["report"] = report
    return row


@router.post("/runs/discover", response_model=DiscoverOut, status_code=202)
def discover(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    wait: bool = False,
    classify: bool = True,
    enrich: bool = True,
) -> Any:
    """Start a discovery run. One at a time; a second request gets 409."""
    lock: threading.Lock = request.app.state.discovery_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A discovery run is already in progress.")

    run_id = store.start_run(db.connection())
    state = request.app.state

    def work() -> dict[str, Any] | None:
        try:
            report = run_discovery(
                db.connection(), settings, run_id=run_id, classify=classify, enrich=enrich
            )
            state.last_report = report.as_dict()
            log.info(report.summary())
            return state.last_report
        except Exception:
            log.exception("discovery run %s failed", run_id)
            return None
        finally:
            lock.release()

    if wait:
        report = work()
        return JSONResponse(
            status_code=200, content=DiscoverOut(run_id=run_id, report=report).model_dump()
        )

    threading.Thread(target=work, name=f"discover-{run_id}", daemon=True).start()
    return DiscoverOut(run_id=run_id)
