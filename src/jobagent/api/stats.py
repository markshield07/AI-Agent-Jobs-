"""The dashboard's numbers: totals, the per-day series, breakdowns, recent activity,
and the job cards on the overview."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from jobagent.api.discovery import job_cards
from jobagent.api.routes import DbDep, SettingsDep
from jobagent.apply import store as applications
from jobagent.discovery import store as jobs
from jobagent.stats import dashboard_stats, recent_activity

router = APIRouter(prefix="/api")


@router.get("/stats")
def stats(
    db: DbDep,
    days: int = Query(30, ge=1, le=366),
    tz_offset_minutes: int = Query(0, ge=-840, le=840, description="Minutes east of UTC."),
) -> dict[str, Any]:
    return dashboard_stats(db.connection(), days=days, tz_offset_minutes=tz_offset_minutes)


@router.get("/activity")
def activity(db: DbDep, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    return recent_activity(db.connection(), limit)


@router.get("/overview")
def overview(db: DbDep, limit: int = Query(6, ge=1, le=50)) -> dict[str, Any]:
    """The overview's job cards: the latest applications sent and the queue ahead."""
    conn = db.connection()
    return {
        "recent": applications.list_applications(conn, submitted=True, limit=limit),
        "up_next": job_cards(conn, jobs.list_jobs(conn, status="queued", limit=limit)),
        "queued": jobs.count_jobs(conn).get("queued", 0),
    }


@router.get("/config")
def config(settings: SettingsDep) -> dict[str, Any]:
    """The settings the dashboard shows. Never a key, a password or a path to one."""
    return {
        "apply_mode": settings.apply_mode,
        "daily_apply_cap": settings.daily_apply_cap,
        "easy_apply_daily_cap": settings.easy_apply_daily_cap,
        "apply_delay_seconds": settings.apply_delay_seconds,
        "llm_backend": settings.llm_backend,
        "inbox_configured": bool(settings.imap_host and settings.imap_user),
    }
