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

Nothing here imports Playwright; the visible browser is opened by
`browser.session.interactive_login`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
