"""Dashboard API for the sign-ins: LinkedIn, Indeed, and each company's
Workday site. Whether each is saved and still good, and a way to delete one.

A Workday sign-in ends on the company's side while its cookies still look
valid, so its `state` comes from what the last run or check met there
(needs_sign_in, signed_in, or unchecked), with the applications waiting on it.

Signing in needs a visible browser on the person's own machine, so there is
no route that starts it; the dashboard shows the `jobagent login <site>`
command instead. Nothing here returns a cookie.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from jobagent.api.routes import SettingsDep
from jobagent.apply import sessions

router = APIRouter(prefix="/api")


@router.get("/sessions")
def list_sessions(settings: SettingsDep) -> list[dict[str, Any]]:
    sites = [
        {**sessions.session_status(settings, name), "login_command": f"jobagent login {name}"}
        for name in sessions.SITES
    ]
    workday = [sessions.workday_status(settings, host) for host in sessions.workday_hosts(settings)]
    return sites + workday


@router.delete("/sessions/workday/{host}")
def forget_workday(host: str, settings: SettingsDep) -> dict[str, Any]:
    try:
        deleted = sessions.forget_workday_session(settings, host)
    except sessions.UnknownSite as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"site": "workday", "host": host, "deleted": deleted}


@router.delete("/sessions/{site}")
def forget(site: str, settings: SettingsDep) -> dict[str, Any]:
    try:
        name = sessions.site(site).name
    except sessions.UnknownSite as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"site": name, "deleted": sessions.forget_session(settings, name)}
