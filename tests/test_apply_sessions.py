"""Saved sign-ins: only the site's own cookies are kept, readable by the owner
only, and a session counts as signed in only while its sign-in cookie lasts."""

from __future__ import annotations

import stat
import time

import pytest

from jobagent.apply import sessions

LATER = time.time() + 30 * 86400


def _state(*cookies):
    return {"cookies": list(cookies), "origins": [{"origin": "https://x", "localStorage": []}]}


def _cookie(name, domain, value="v", expires=LATER):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


def test_sites_are_linkedin_and_indeed():
    assert set(sessions.SITES) == {"linkedin", "indeed"}
    assert sessions.site(" LinkedIn ").name == "linkedin"
    with pytest.raises(sessions.UnknownSite):
        sessions.site("monster")


def test_signed_in_needs_an_unexpired_sign_in_cookie():
    assert sessions.signed_in([_cookie("li_at", ".www.linkedin.com")], "linkedin")
    assert sessions.signed_in([_cookie("li_at", ".linkedin.com", expires=-1)], "linkedin")
    assert not sessions.signed_in([_cookie("li_at", ".linkedin.com", expires=1.0)], "linkedin")
    assert not sessions.signed_in([_cookie("li_at", ".linkedin.com", value="")], "linkedin")
    assert not sessions.signed_in([_cookie("bcookie", ".linkedin.com")], "linkedin")
    assert sessions.signed_in([_cookie("SHOE", ".indeed.com")], "indeed")


def test_saving_keeps_the_sites_cookies_only_owner_readable(settings):
    state = _state(
        _cookie("li_at", ".www.linkedin.com", value="secret"),
        _cookie("JSESSIONID", "www.linkedin.com"),
        _cookie("tracker", ".doubleclick.net"),
        _cookie("CTK", ".indeed.com"),
        _cookie("evil", ".notlinkedin.com"),
    )
    path = sessions.save_session(settings, "linkedin", state)

    assert path == settings.data_dir / "sessions" / "linkedin.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    kept = sessions.load_session(settings, "linkedin")
    assert sorted(c["name"] for c in kept) == ["JSESSIONID", "li_at"]
    assert "localStorage" not in path.read_text() and "doubleclick" not in path.read_text()


def test_a_browser_that_never_signed_in_is_not_saved(settings):
    with pytest.raises(ValueError, match="not signed in to LinkedIn"):
        sessions.save_session(settings, "linkedin", _state(_cookie("bcookie", ".linkedin.com")))
    assert not sessions.session_path(settings, "linkedin").exists()


def test_every_saved_site_goes_into_the_run(settings):
    sessions.save_session(settings, "linkedin", _state(_cookie("li_at", ".linkedin.com")))
    sessions.save_session(settings, "indeed", _state(_cookie("PPID", ".indeed.com")))
    assert sorted(c["name"] for c in sessions.saved_cookies(settings)) == ["PPID", "li_at"]


def test_an_unreadable_file_is_no_session(settings):
    path = sessions.session_path(settings, "indeed")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert sessions.load_session(settings, "indeed") == []
    assert sessions.session_status(settings, "indeed")["signed_in"] is False


def test_status_and_forget(settings):
    assert sessions.session_status(settings, "linkedin") == {
        "site": "linkedin",
        "label": "LinkedIn",
        "saved": False,
        "signed_in": False,
        "saved_at": None,
        "expires_at": None,
    }
    sessions.save_session(settings, "linkedin", _state(_cookie("li_at", ".linkedin.com")))
    status = sessions.session_status(settings, "linkedin")
    assert status["saved"] and status["signed_in"]
    assert status["saved_at"] and status["expires_at"]

    assert sessions.forget_session(settings, "linkedin") is True
    assert sessions.forget_session(settings, "linkedin") is False
    assert sessions.saved_cookies(settings) == []
