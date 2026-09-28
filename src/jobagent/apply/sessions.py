"""Signed-in sessions for the sites that apply from inside an account.

LinkedIn Easy Apply and Indeed's own application flow only exist for someone
who is signed in, so the agent needs a session there. It never needs the
password: `jobagent login linkedin` opens a visible browser at the site's own
sign-in page, the person signs in by hand (including any two-step check), and
what is kept is the browser's cookies for that site, written to
`data/sessions/<site>.json` with owner-only permissions. Every apply run loads
them into its browser.

That file is as good as being signed in, which is why it lives under `data/`
(gitignored), is readable only by its owner, and can be deleted with
`jobagent login <site> --forget`. Signing out of the site in any browser, or
changing the password, ends it.

Workday is different in one way: there is no Workday account, only one per
company that uses it (crowdstrike.wd5.myworkdayjobs.com, nvidia.wd5...). So
`jobagent login workday <posting URL>` signs in to that company's site and
keeps its cookies in `data/sessions/workday/<host>.json`, one file per
company, under the same rules. Workday does not name a sign-in cookie the
agent could check, so the person says when they are signed in (Enter in the
terminal), and a stale file shows up as a sign-in page during the next run.

Nothing here imports Playwright; the visible browser is opened by
`browser.session.interactive_login`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from jobagent.config import Settings


@dataclass(frozen=True, slots=True)
class Site:
    name: str
    label: str
    login_url: str
    domain: str  # cookies for this domain and its subdomains are kept
    auth_cookies: tuple[str, ...]  # any one of these present means signed in


SITES: dict[str, Site] = {
    "linkedin": Site(
        name="linkedin",
        label="LinkedIn",
        login_url="https://www.linkedin.com/login",
        domain="linkedin.com",
        auth_cookies=("li_at",),
    ),
    "indeed": Site(
        name="indeed",
        label="Indeed",
        login_url="https://secure.indeed.com/auth",
        domain="indeed.com",
        auth_cookies=("PPID", "SOCK", "SHOE"),
    ),
}


class UnknownSite(ValueError):
    pass


def site(name: str) -> Site:
    try:
        return SITES[name.strip().lower()]
    except KeyError as exc:
        raise UnknownSite(f"no sign-in for {name!r}; one of: {', '.join(SITES)}") from exc


def sessions_dir(settings: Settings) -> Path:
    return settings.data_dir / "sessions"


def session_path(settings: Settings, name: str) -> Path:
    return sessions_dir(settings) / f"{site(name).name}.json"


def _belongs(cookie: dict[str, Any], s: Site) -> bool:
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    return domain == s.domain or domain.endswith("." + s.domain)


def site_cookies(state: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """The cookies in a browser state that belong to one site, nothing else."""
    s = site(name)
    return [c for c in state.get("cookies") or [] if _belongs(c, s)]


def signed_in(cookies: list[dict[str, Any]], name: str, *, now: float | None = None) -> bool:
    """Whether these cookies carry a sign-in for the site that has not expired."""
    s = site(name)
    now = datetime.now(UTC).timestamp() if now is None else now
    for cookie in cookies:
        if cookie.get("name") in s.auth_cookies and cookie.get("value"):
            expires = cookie.get("expires")
            if expires in (None, -1) or float(expires) > now:
                return True
    return False


def save_session(settings: Settings, name: str, state: dict[str, Any]) -> Path:
    """Keep the site's cookies from a browser state, readable by the owner only."""
    s = site(name)
    cookies = site_cookies(state, s.name)
    if not signed_in(cookies, s.name):
        raise ValueError(f"that browser is not signed in to {s.label}")
    path = session_path(settings, s.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = {"site": s.name, "saved_at": _now(), "cookies": cookies}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    os.chmod(path, 0o600)
    return path


def load_session(settings: Settings, name: str) -> list[dict[str, Any]]:
    """The saved cookies for a site, or [] when there are none or the file is unreadable."""
    path = session_path(settings, name)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return site_cookies(payload, name)


def saved_cookies(settings: Settings) -> list[dict[str, Any]]:
    """Every saved site's cookies, for loading into an apply run's browser."""
    cookies: list[dict[str, Any]] = []
    for name in SITES:
        cookies.extend(load_session(settings, name))
    for host in workday_hosts(settings):
        cookies.extend(load_workday_session(settings, host))
    return cookies


def forget_session(settings: Settings, name: str) -> bool:
    path = session_path(settings, name)
    if path.exists():
        path.unlink()
        return True
    return False


def session_status(settings: Settings, name: str) -> dict[str, Any]:
    """What the dashboard and `jobagent login --status` show for one site."""
    s = site(name)
    path = session_path(settings, s.name)
    cookies = load_session(settings, s.name)
    saved_at = None
    if path.exists():
        try:
            saved_at = json.loads(path.read_text()).get("saved_at")
        except (OSError, ValueError):
            saved_at = None
    expiries = [
        float(c["expires"])
        for c in cookies
        if c.get("name") in s.auth_cookies and c.get("expires") not in (None, -1)
    ]
    return {
        "site": s.name,
        "label": s.label,
        "saved": path.exists(),
        "signed_in": signed_in(cookies, s.name),
        "saved_at": saved_at,
        "expires_at": (
            datetime.fromtimestamp(max(expiries), UTC).isoformat(timespec="seconds")
            if expiries
            else None
        ),
    }


# ------------------------------------------------------------------ workday --

WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com")


def workday_host(url: str) -> str:
    """The company's Workday host in a posting URL. Raises UnknownSite for anything else."""
    host = (urlparse(url if "//" in url else f"https://{url}").hostname or "").lower()
    if not any(host.endswith("." + d) for d in WORKDAY_DOMAINS):
        raise UnknownSite(
            f"{url!r} is not a Workday posting; give the job's own link, e.g. "
            "https://acme.wd5.myworkdayjobs.com/careers/job/..."
        )
    return host


def _workday_file(host: str) -> str:
    if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
        raise UnknownSite(f"not a Workday host: {host!r}")
    return f"{host}.json"


def workday_dir(settings: Settings) -> Path:
    return sessions_dir(settings) / "workday"


def workday_session_path(settings: Settings, host: str) -> Path:
    return workday_dir(settings) / _workday_file(host)


def workday_cookies(state: dict[str, Any], host: str) -> list[dict[str, Any]]:
    """The cookies in a browser state that this company's Workday site can read:
    its own host's, and those set for a parent of it short of the bare suffix."""
    out = []
    for cookie in state.get("cookies") or []:
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        if "." in domain and (host == domain or host.endswith("." + domain)):
            out.append(cookie)
    return out


def _workday_payload(settings: Settings, host: str) -> dict[str, Any]:
    try:
        payload = json.loads(workday_session_path(settings, host).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_workday_session(
    settings: Settings, host: str, state: dict[str, Any], *, url: str | None = None
) -> Path:
    """Keep one company's Workday cookies, readable by the owner only.

    `url` is the posting signed in from, kept so the sign-in can be checked
    later. The applications waiting on this sign-in are kept too; a fresh
    sign-in clears any earlier "signed out" mark."""
    cookies = workday_cookies(state, host)
    if not cookies:
        raise ValueError(f"the browser holds no cookies for {host}; sign in there first")
    old = _workday_payload(settings, host)
    path = workday_session_path(settings, host)
    _write_private(
        path,
        {
            "site": "workday",
            "host": host,
            "saved_at": _now_exact(),
            "url": url or old.get("url"),
            "waiting": list(old.get("waiting") or []),
            "cookies": cookies,
        },
    )
    return path


def load_workday_session(settings: Settings, host: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(workday_session_path(settings, host).read_text())
    except (OSError, ValueError, UnknownSite):
        return []
    return workday_cookies(payload, host)


def workday_hosts(settings: Settings) -> list[str]:
    """The companies with a saved Workday sign-in, or with applications waiting for one."""
    folder = workday_dir(settings)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if re.fullmatch(r"[a-z0-9.-]+", p.stem))


def forget_workday_session(settings: Settings, host: str) -> bool:
    path = workday_session_path(settings, host)
    if path.exists():
        path.unlink()
        return True
    return False


def mark_workday_signed_out(
    settings: Settings, host: str, *, job_id: str | None = None, url: str | None = None
) -> None:
    """A run met this company's sign-in page: the saved sign-in no longer works.

    Workday ends a session on the company's side after about an hour, while
    its cookies still look valid, so this is the only sure sign. The job is
    kept as waiting, to be tried again right after the next sign-in."""
    payload = _workday_payload(settings, host)
    waiting = list(payload.get("waiting") or [])
    if job_id and job_id not in waiting:
        waiting.append(job_id)
    payload.update(
        {
            "site": "workday",
            "host": host,
            "signed_out_at": _now_exact(),
            "url": payload.get("url") or url,
            "waiting": waiting,
            "cookies": payload.get("cookies") or [],
        }
    )
    _write_private(workday_session_path(settings, host), payload)


def record_workday_check(settings: Settings, host: str, state: str) -> None:
    """What opening the company's site with the saved sign-in showed:
    signed_in or signed_out. Nothing is recorded for a host never signed in to."""
    payload = _workday_payload(settings, host)
    if not payload:
        return
    payload["checked_at"] = _now_exact()
    payload["checked"] = state
    if state == "signed_out":
        payload["signed_out_at"] = payload["checked_at"]
    _write_private(workday_session_path(settings, host), payload)


def refresh_workday_session(settings: Settings, host: str, state: dict[str, Any]) -> bool:
    """Keep the cookies a signed-in visit to the company's site left, and
    record the visit as a check that the sign-in works. The time of the
    sign-in itself (saved_at) stays. False when the browser held none."""
    payload = _workday_payload(settings, host)
    cookies = workday_cookies(state, host)
    if not payload or not cookies:
        return False
    payload["cookies"] = cookies
    payload["checked_at"] = _now_exact()
    payload["checked"] = "signed_in"
    payload["refreshed_at"] = payload["checked_at"]
    _write_private(workday_session_path(settings, host), payload)
    return True


def record_keep_alive(
    settings: Settings, host: str, state: str, *, how: str = "", landed: str = ""
) -> None:
    """What a keep-alive visit found (signed_in, signed_out, unknown, timed_out
    or error), when, how it visited and where the page ended up, so
    `login workday --status` and the dashboard show the keep-alive is working.
    A signed-out visit also marks the sign-in as ended."""
    payload = _workday_payload(settings, host)
    if not payload:
        return
    payload["kept_alive_at"] = _now_exact()
    payload["keep_alive"] = state
    payload["keep_alive_how"] = how
    payload["keep_alive_landed"] = landed
    if state == "signed_out":
        payload["checked_at"] = payload["kept_alive_at"]
        payload["checked"] = "signed_out"
        payload["signed_out_at"] = payload["kept_alive_at"]
    _write_private(workday_session_path(settings, host), payload)


def save_workday_capture(settings: Settings, host: str, name: str, html: str) -> Path:
    """Keep a page from this company's site for a later look, owner-only:
    data/sessions/workday/<host>-<name>.html."""
    path = workday_dir(settings) / f"{host}-{name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(html)
    os.chmod(path, 0o600)
    return path


def workday_waiting(settings: Settings, host: str) -> list[str]:
    """Jobs whose application stopped at this company's sign-in page."""
    return [str(j) for j in _workday_payload(settings, host).get("waiting") or []]


def clear_workday_waiting(settings: Settings, host: str, job_ids: list[str]) -> None:
    payload = _workday_payload(settings, host)
    if not payload:
        return
    payload["waiting"] = [j for j in payload.get("waiting") or [] if j not in set(job_ids)]
    _write_private(workday_session_path(settings, host), payload)


def workday_status(settings: Settings, host: str) -> dict[str, Any]:
    """Where one company's Workday sign-in stands.

    `state` is not_signed_in (nothing saved), needs_sign_in (a run or a check
    met the sign-in page after the last sign-in), signed_in (a run or a check
    got past it since), or unchecked (saved, not tried since). Cookies alone
    cannot tell: Workday ends the session on its side while they still look
    valid."""
    payload = _workday_payload(settings, host)
    cookies = load_workday_session(settings, host)
    now = datetime.now(UTC).timestamp()
    live = [c for c in cookies if c.get("expires") in (None, -1) or float(c["expires"]) > now]
    saved_at = payload.get("saved_at") if cookies else None
    signed_out_at = payload.get("signed_out_at")
    checked_at = payload.get("checked_at")
    # The latest word since the last sign-in decides.
    since = [
        (at, verdict)
        for at, verdict in (
            (signed_out_at, "needs_sign_in"),
            (checked_at, "signed_in" if payload.get("checked") == "signed_in" else ""),
        )
        if at and verdict and (not saved_at or at >= saved_at)
    ]
    if not cookies:
        state = "not_signed_in"
    elif since:
        state = max(since)[1]
    else:
        state = "unchecked"
    url = payload.get("url")
    return {
        "site": "workday",
        "host": host,
        "label": f"Workday: {host.split('.')[0]}",
        "saved": bool(cookies),
        "saved_at": saved_at,
        "cookies": len(live),
        "state": state,
        "signed_in": state == "signed_in",
        "checked_at": checked_at,
        "signed_out_at": signed_out_at,
        "kept_alive_at": payload.get("kept_alive_at"),
        "keep_alive": payload.get("keep_alive"),
        "url": url,
        "waiting": len(payload.get("waiting") or []),
        "login_command": f"jobagent login workday {url or '<posting URL>'}",
    }


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for folder in (path.parent, path.parent.parent):
        if folder.name in ("sessions", "workday"):
            try:
                os.chmod(folder, 0o700)
            except OSError:
                pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    os.chmod(path, 0o600)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_exact() -> str:
    # Workday's sign-in, sign-out and check times are compared to tell which came last.
    return datetime.now(UTC).isoformat(timespec="microseconds")
