"""The dashboard: its API, the files it is served from, and the page itself in
a real browser against a running server with a small history in it."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.main import create_app


def _seed(conn):
    """Two applications sent, one with a screening reply; one queued job; one
    application waiting on a question."""
    ids = []
    for n, (category, source) in enumerate(
        [
            ("Platform Engineering", "linkedin"),
            ("Backend Engineering", "greenhouse"),
            ("Backend Engineering", "lever"),
            ("Data Engineering", "indeed"),
        ],
        start=1,
    ):
        raw = RawJob(
            url=f"https://example.com/jobs/{n}",
            title=f"Engineer <b>{n}</b>",
            company=f"Co {n}",
            source=source,
            location="Menifee, CA",
            description=(
                "**About the Role**\n\nYou will run <i>the</i> network for our offices.\n\n"
                "**Responsibilities**\n\n* Keep BGP healthy\n* Lead the NOC"
            ),
            salary_min=120000 if n == 1 else None,
            salary_max=150000 if n == 1 else None,
        )
        jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
        conn.execute(
            "UPDATE jobs SET category = ?, score = ? WHERE id = ?", (category, 70 + n, jid)
        )
        ids.append(jid)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    apps = []
    for jid, ats in zip(ids[:2], ("linkedin", "greenhouse"), strict=True):
        cur = conn.execute(
            "INSERT INTO applications (job_id, mode, ats, submitted_at, created_at)"
            " VALUES (?, 'auto', ?, ?, ?)",
            (jid, ats, now, now),
        )
        apps.append(cur.lastrowid)
        jobs.set_status(conn, jid, "applied")
    conn.execute(
        "INSERT INTO application_events (application_id, kind, to_status, source, created_at)"
        " VALUES (?, 'status_change', 'screening', 'email', ?)",
        (apps[0], now),
    )
    cur = conn.execute(
        "INSERT INTO applications (job_id, mode, ats, created_at)"
        " VALUES (?, 'dry_run', 'lever', ?)",
        (ids[2], now),
    )
    conn.execute(
        "INSERT INTO submission_attempts (application_id, mode, outcome, handler, filled,"
        " needed, fields, started_at, ended_at)"
        " VALUES (?, 'dry_run', 'needs_input', 'lever', '[]', ?, '[]', ?, ?)",
        (
            cur.lastrowid,
            '[{"key": "k8s", "label": "Years of Kubernetes?", "kind": "text", "required": true,'
            ' "options": [], "reason": "nothing on file", "answer_key": "q:years of kubernetes"}]',
            now,
            now,
        ),
    )
    jobs.set_status(conn, ids[3], "queued")
    conn.commit()
    return apps


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------- api --


def test_stats_activity_and_config_routes(client, conn, settings):
    _seed(conn)
    stats = client.get("/api/stats", params={"days": 7, "tz_offset_minutes": -300}).json()
    assert stats["totals"]["applied"] == 2 and stats["totals"]["responses"] == 1
    assert len(stats["per_day"]) == 7
    assert stats["attention"]["needs_input"] == 1
    assert client.get("/api/stats", params={"days": 0}).status_code == 422

    feed = client.get("/api/activity", params={"limit": 3}).json()
    assert [i["kind"] for i in feed] == ["applied", "applied"], "the inbox's move shows on a reply"

    config = client.get("/api/config")
    assert config.json()["apply_mode"] == "dry_run"
    assert config.json()["easy_apply_daily_cap"] == 10
    assert config.json()["dice_daily_cap"] == 3 and config.json()["apply_sites"] == []
    assert settings.anthropic_api_key not in config.text


def test_the_dashboard_is_served_beside_the_api(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<script src="app.js">' in page.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}, "the API still wins"
    # Nothing is loaded from another origin.
    for name in ("/", "/app.js", "/style.css"):
        body = client.get(name).text
        assert "https://" not in body.replace("https://' + ", ""), name


# --------------------------------------------------------------- browser --


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(settings, conn):
    import uvicorn

    apps = _seed(conn)
    port = _free_port()
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "apps": apps}
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.mark.usefixtures("page")
def test_the_page_renders_the_numbers_and_every_tab(page, server):
    errors: list[str] = []
    failed: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: (
            errors.append(msg.text)
            if msg.type == "error" and not msg.text.startswith("Failed to load resource")
            else None
        ),
    )
    page.on("response", lambda r: failed.append(r.url) if r.status >= 400 else None)

    page.goto(server["url"] + "/#overview")
    page.wait_for_selector("#kpis .kpi")
    page.wait_for_selector("#recent .job-card")
    kpis = dict(
        zip(
            page.locator("#kpis .kpi .label").all_inner_texts(),
            page.locator("#kpis .kpi .value").all_inner_texts(),
            strict=True,
        )
    )
    assert kpis["Applied"] == "2" and kpis["Heard back"] == "1" and kpis["Jobs found"] == "4"
    assert page.locator("#per-day svg rect.bar-applied").count() == 30
    assert "need your answers" in page.inner_text("#attention")
    assert "Platform Engineering" in page.inner_text("#categories")
    assert "LinkedIn" in page.inner_text("#sites")
    assert page.inner_text("#mode-badge") == "Dry run"

    # Job cards: the newest sent first, pay as listed or said to be missing,
    # the posting's own words as text.
    recent = page.locator("#recent .job-card")
    assert recent.count() == 2
    assert "Engineer <b>1</b>" in recent.nth(1).inner_text()
    assert "$120k – $150k" in recent.nth(1).inner_text()
    assert "Salary not listed" in recent.nth(0).inner_text()
    assert "You will run <i>the</i> network" in recent.nth(0).inner_text()
    assert page.locator("#recent i, #recent b").count() == 0
    assert page.locator("#up-next .job-card").count() == 1
    assert "Engineer <b>4</b>" in page.inner_text("#up-next")

    page.click("#up-next .job-card")
    page.wait_for_selector("#drawer:not([hidden]) .points")
    assert "Keep BGP healthy" in page.inner_text("#drawer")
    assert "Salary not listed" in page.inner_text("#drawer")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer", state="hidden")

    page.click("#range button[data-days='7']")
    page.wait_for_function("document.querySelectorAll('#per-day rect.bar-applied').length === 7")

    page.click("a[data-tab='applications']")
    page.wait_for_selector("#app-table tbody tr")
    assert page.locator("#app-table tbody tr").count() == 3
    # A title from a job board is text, never markup.
    assert "Engineer <b>1</b>" in page.inner_text("#app-table")
    assert page.locator("#app-table b").count() == 0

    page.click("#app-filters button:has-text('Needs answers')")
    page.wait_for_function("document.querySelectorAll('#app-table tbody tr').length === 1")
    page.click("#app-table tbody tr")
    page.wait_for_selector("#app-detail:not([hidden]) .question")
    assert "Years of Kubernetes?" in page.inner_text("#app-detail")

    page.click("a[data-tab='jobs']")
    page.wait_for_selector("#job-list .job-card")
    assert "Engineer <b>4</b>" in page.inner_text("#job-list")
    page.fill("#job-search", "nothing like this")
    page.wait_for_selector("#job-empty:not([hidden])")
    assert page.locator("#job-list .job-card").count() == 0

    page.click("a[data-tab='settings']")
    page.wait_for_selector("#sessions .session")
    assert "jobagent login linkedin" in page.inner_text("#sessions")
    assert "Dry run" in page.inner_text("#config")

    # Wait for the resume request itself: the page's words can show before
    # its response event reaches the test, which then read no miss at all.
    with page.expect_response(lambda r: r.url.endswith("/api/resume")) as resume:
        page.click("a[data-tab='profile']")
    assert resume.value.status == 404
    page.wait_for_selector("#resume-current")
    assert "No resume uploaded yet." in page.inner_text("#resume-current")

    assert errors == [], errors
    # The one expected miss: no resume uploaded, which the page says in words.
    assert [u.split("/", 3)[3] for u in failed] == ["api/resume"], failed


@pytest.mark.usefixtures("page")
def test_a_lapsed_workday_sign_in_is_flagged_with_its_waiting_jobs(page, server, settings):
    from jobagent.apply import sessions

    host = "crowdstrike.wd5.myworkdayjobs.com"
    posting = f"https://{host}/crowdstrikecareers/job/USA-Remote/Manager_R1"
    cookie = {"name": "PLAY_SESSION", "value": "x", "domain": host, "expires": -1}
    sessions.save_workday_session(settings, host, {"cookies": [cookie]}, url=posting)
    sessions.record_keep_alive(settings, host, "unknown", how="candidate home")
    sessions.mark_workday_signed_out(settings, host, job_id="j1")
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(server["url"] + "/#overview")
    page.wait_for_selector("#attention a[href='#settings']")
    assert "Workday sign-in to renew (1 waiting)" in page.inner_text("#attention")

    page.click("#attention a[href='#settings']")
    page.wait_for_selector("#sessions .session:has-text('crowdstrike')")
    row = page.inner_text("#sessions .session:has-text('crowdstrike')")
    assert "Sign in again" in row and f"jobagent login workday {posting}" in row
    assert "1 application wait" in row
    assert "Keep-alive last visited" in row and "could not tell" in row
    assert errors == [], errors


@pytest.mark.usefixtures("page")
def test_recording_an_interview_updates_the_application(page, server):
    page.goto(server["url"] + f"/#applications?id={server['apps'][1]}")
    page.wait_for_selector("#app-detail:not([hidden]) select")
    page.select_option("#app-detail select", "interviewing")
    page.click("#app-detail button:has-text('Save')")
    page.wait_for_function(
        "document.querySelector('#app-detail') && "
        "document.querySelector('#app-detail').innerText.includes('Interviewing')"
    )


@pytest.mark.usefixtures("page")
def test_the_resume_tab_lists_your_skills_and_turns_one_off(page, server):
    page.request.post(server["url"] + "/api/facts/keywords", data={"keywords": ["RMA", "Python"]})

    page.goto(server["url"] + "/#profile")
    page.wait_for_selector("#skills li:has-text('RMA')")
    assert page.inner_text("#skill-count") == "(2)"
    assert page.locator("#resume-form input[name='reparse']").count() == 1

    page.click("#skills li:has-text('Python') button")
    page.wait_for_function("document.querySelector('#skill-count').textContent === '(1)'")
    assert "Python" not in page.inner_text("#skills")
