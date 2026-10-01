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


def test_sites_are_linkedin_indeed_and_dice():
    assert set(sessions.SITES) == {"linkedin", "indeed", "dice"}
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


def test_dice_counts_any_of_its_own_unexpired_cookies_as_signed_in(settings):
    # Dice's sign-in cookie has no known name: the person said they were
    # signed in, and the cookies Dice left are what is kept.
    assert sessions.site("dice").auth_cookies == ()
    assert sessions.signed_in([_cookie("anything", ".dice.com")], "dice")
    assert not sessions.signed_in([_cookie("anything", ".dice.com", expires=1.0)], "dice")
    assert not sessions.signed_in([_cookie("li_at", ".linkedin.com")], "dice")
    assert not sessions.signed_in([], "dice")

    sessions.save_session(
        settings, "dice", _state(_cookie("s", ".dice.com"), _cookie("li_at", ".linkedin.com"))
    )
    assert [c["name"] for c in sessions.load_session(settings, "dice")] == ["s"]
    status = sessions.session_status(settings, "dice")
    assert status["signed_in"] and status["expires_at"] is None
    with pytest.raises(ValueError):
        sessions.save_session(settings, "dice", _state(_cookie("li_at", ".linkedin.com")))


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


# ------------------------------------------------------------------ workday --

WORKDAY_POSTING = "https://acme.wd5.myworkdayjobs.com/careers/job/Remote/Engineer_R1"


def test_a_workday_sign_in_is_per_company(settings):
    assert sessions.workday_host(WORKDAY_POSTING) == "acme.wd5.myworkdayjobs.com"
    assert sessions.workday_host("globex.wd1.myworkdaysite.com/recruiting") == (
        "globex.wd1.myworkdaysite.com"
    )
    with pytest.raises(sessions.UnknownSite):
        sessions.workday_host("https://www.linkedin.com/jobs/view/1/")
    with pytest.raises(sessions.UnknownSite):
        sessions.workday_session_path(settings, "../../etc/passwd")


def test_saving_a_workday_sign_in_keeps_that_companys_cookies_only(settings):
    host = "acme.wd5.myworkdayjobs.com"
    state = _state(
        _cookie("PLAY_SESSION", "acme.wd5.myworkdayjobs.com"),
        _cookie("wd-browser-id", ".myworkdayjobs.com"),
        _cookie("PLAY_SESSION", "globex.wd1.myworkdayjobs.com"),
        _cookie("li_at", ".linkedin.com"),
        _cookie("tracker", ".com"),
    )
    path = sessions.save_workday_session(settings, host, state)

    assert path == settings.data_dir / "sessions" / "workday" / f"{host}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    kept = sessions.load_workday_session(settings, host)
    assert sorted(c["domain"] for c in kept) == [".myworkdayjobs.com", "acme.wd5.myworkdayjobs.com"]
    assert "globex" not in path.read_text() and "li_at" not in path.read_text()
    assert sessions.workday_hosts(settings) == [host]
    assert sessions.workday_status(settings, host)["cookies"] == 2
    assert {c["domain"] for c in sessions.saved_cookies(settings)} >= {"acme.wd5.myworkdayjobs.com"}

    assert sessions.forget_workday_session(settings, host)
    assert sessions.workday_hosts(settings) == []
    assert not sessions.forget_workday_session(settings, host)


def test_a_workday_browser_with_no_cookies_for_the_company_saves_nothing(settings):
    with pytest.raises(ValueError, match="no cookies"):
        sessions.save_workday_session(
            settings, "acme.wd5.myworkdayjobs.com", _state(_cookie("li_at", ".linkedin.com"))
        )
    assert sessions.workday_hosts(settings) == []


def test_a_workday_sign_in_is_judged_by_what_the_site_showed_not_its_cookies(settings):
    """CrowdStrike ended a sign-in after about an hour while its 11 cookies still
    looked valid: the state comes from the last run or check on the site."""
    host = "acme.wd5.myworkdayjobs.com"
    assert sessions.workday_status(settings, host)["state"] == "not_signed_in"

    state = _state(_cookie("PLAY_SESSION", host))
    sessions.save_workday_session(settings, host, state, url=WORKDAY_POSTING)
    status = sessions.workday_status(settings, host)
    assert status["state"] == "unchecked" and status["cookies"] == 1
    assert status["login_command"] == f"jobagent login workday {WORKDAY_POSTING}"

    sessions.record_workday_check(settings, host, "signed_in")
    assert sessions.workday_status(settings, host)["state"] == "signed_in"

    sessions.mark_workday_signed_out(settings, host, job_id="j1")
    sessions.mark_workday_signed_out(settings, host, job_id="j2")
    sessions.mark_workday_signed_out(settings, host, job_id="j1")
    status = sessions.workday_status(settings, host)
    assert status["state"] == "needs_sign_in" and status["cookies"] == 1
    assert sessions.workday_waiting(settings, host) == ["j1", "j2"]

    # A fresh sign-in clears the mark and keeps the jobs waiting for it.
    sessions.save_workday_session(settings, host, state)
    status = sessions.workday_status(settings, host)
    assert status["state"] == "unchecked" and status["url"] == WORKDAY_POSTING
    assert sessions.workday_waiting(settings, host) == ["j1", "j2"]
    sessions.clear_workday_waiting(settings, host, ["j1"])
    assert sessions.workday_waiting(settings, host) == ["j2"]

    sessions.record_workday_check(settings, host, "signed_out")
    assert sessions.workday_status(settings, host)["state"] == "needs_sign_in"


def test_a_job_can_wait_on_a_company_never_signed_in_to(settings):
    host = "globex.wd1.myworkdayjobs.com"
    sessions.mark_workday_signed_out(settings, host, job_id="j9", url=f"https://{host}/x/job/1")
    status = sessions.workday_status(settings, host)
    assert status["state"] == "not_signed_in" and not status["saved"]
    assert status["waiting"] == 1 and status["login_command"].endswith(f"https://{host}/x/job/1")
    sessions.record_workday_check(settings, host, "signed_in")  # nothing saved: ignored
    assert sessions.workday_status(settings, host)["state"] == "not_signed_in"
    assert oct(sessions.workday_session_path(settings, host).stat().st_mode)[-3:] == "600"
