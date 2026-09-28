"""Dashboard API for the LinkedIn and Indeed sign-ins: whether each is saved
and still good, and a way to delete one.

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
    return [
        {**sessions.session_status(settings, name), "login_command": f"jobagent login {name}"}
        for name in sessions.SITES
    ]


@router.delete("/sessions/{site}")
def forget(site: str, settings: SettingsDep) -> dict[str, Any]:
    try:
        name = sessions.site(site).name
    except sessions.UnknownSite as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"site": name, "deleted": sessions.forget_session(settings, name)}
