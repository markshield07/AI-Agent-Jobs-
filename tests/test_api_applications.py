"""The submission end of the API: start a pass, read applications, answer what
a form asked, approve one, log what happened after.

The apply run itself is stubbed: these tests are about the routes, the lock
that keeps two runs out of one browser, and the shapes the dashboard reads.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from jobagent.apply import store
from jobagent.apply.models import Fill, HandlerResult, NeededInput
from jobagent.apply.pipeline import ApplyError, ApplyReport
from jobagent.db.database import utcnow
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.main import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def conn(client):
    return client.app.state.db.connection()


def _job(conn, url="https://jobs.lever.co/acme/1", title="Engineer", company="Acme") -> str:
    raw = RawJob(url=url, title=title, company=company, source="lever", ats_type="lever")
    jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    jobs.set_status(conn, jid, "queued")
    return jid


ASKED = NeededInput(
    key="q_auth",
    label="Authorized to work?",
    kind="select",
    required=True,
    options=["Yes", "No"],
    reason="never guessed",
    answer_key="work_authorization",
)


def _parked(conn, job_id: str) -> int:
    """An application that stopped on a question."""
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run", ats="lever")
    store.record_attempt(
        conn,
        app_id,
        HandlerResult(
            outcome="needs_input",
            needed=[ASKED],
            filled=[Fill(key="name", value="Mark Shield", label="Full name")],
            screenshot_path=None,
        ),
        mode="dry_run",
        handler="lever",
        started_at=utcnow(),
    )
    return app_id


# ------------------------------------------------------------------ runs --


def test_apply_runs_and_returns_the_report_when_asked_to_wait(client, monkeypatch):
    seen = {}

    def fake(conn, settings, **kwargs):
        seen.update(kwargs)
        report = ApplyReport(run_id=1, mode=kwargs["mode"] or "dry_run", considered=2, dry_run=2)
        report.results = [{"job_id": "j1", "outcome": "dry_run"}]
        return report

    monkeypatch.setattr("jobagent.api.applications.run_apply", fake)
    response = client.post(
        "/api/apply", params={"wait": "true"}, json={"job_ids": ["j1", "j2"], "mode": "review"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True and body["report"]["dry_run"] == 2
    assert seen["job_ids"] == ["j1", "j2"] and seen["mode"] == "review"

    last = client.get("/api/apply/last")
    assert last.status_code == 200 and last.json()["considered"] == 2


def test_apply_without_waiting_starts_it_in_the_background(client, monkeypatch):
    started = threading.Event()

    def fake(conn, settings, **kwargs):
        started.set()
        return ApplyReport(run_id=1, mode="dry_run", considered=0)

    monkeypatch.setattr("jobagent.api.applications.run_apply", fake)
    response = client.post("/api/apply", json={})
    assert response.status_code == 202 and response.json() == {"started": True, "report": None}
    assert started.wait(5), "the run was started"


def test_no_apply_run_yet(client):
    assert client.get("/api/apply/last").status_code == 404


def test_a_second_run_is_refused_while_one_holds_the_browser(client, monkeypatch):
    client.app.state.apply_lock.acquire()
    try:
        assert client.post("/api/apply", json={}).status_code == 409
    finally:
        client.app.state.apply_lock.release()


def test_an_unknown_mode_is_refused(client):
    assert client.post("/api/apply", json={"mode": "yolo"}).status_code == 422
    assert client.post("/api/apply", json={"limit": 0}).status_code == 422


def test_a_failed_run_releases_the_browser(client, monkeypatch):
    def boom(conn, settings, **kwargs):
        raise RuntimeError("no browser")

    monkeypatch.setattr("jobagent.api.applications.run_apply", boom)
    response = client.post("/api/apply", params={"wait": "true"}, json={})
    assert response.status_code == 200 and response.json()["report"] is None
    assert client.app.state.apply_lock.acquire(blocking=False), "the lock was let go"
    client.app.state.apply_lock.release()


# ---------------------------------------------------------- applications --


def test_applications_are_listed_with_what_they_wait_on(client, conn):
    app_id = _parked(conn, _job(conn))

    rows = client.get("/api/applications").json()
    assert len(rows) == 1
    assert rows[0]["id"] == app_id and rows[0]["status"] == "needs_input"
    assert rows[0]["job"]["title"] == "Engineer"
    assert rows[0]["needed"][0]["answer_key"] == "work_authorization"

    assert client.get("/api/applications", params={"status": "needs_input"}).json()
    assert client.get("/api/applications", params={"status": "applied"}).json() == []
    assert client.get("/api/applications", params={"status": "nonsense"}).status_code == 400
    assert client.get("/api/applications/counts").json() == {"needs_input": 1}


def test_one_application_comes_with_its_attempts_and_events(client, conn):
    app_id = _parked(conn, _job(conn))
    store.add_event(conn, app_id, kind="note", note="called them")

    body = client.get(f"/api/applications/{app_id}").json()
    assert body["status"] == "needs_input"
    assert len(body["attempts"]) == 1 and body["attempts"][0]["handler"] == "lever"
    assert body["attempts"][0]["filled"][0]["value"] == "Mark Shield"
    assert [e["note"] for e in body["events"]] == ["called them"]

    assert client.get("/api/applications/999").status_code == 404


def test_a_screenshot_is_served_when_there_is_one(client, conn, tmp_path):
    job_id = _job(conn)
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run", ats="lever")
    assert client.get(f"/api/applications/{app_id}/screenshot").status_code == 404

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    store.record_attempt(
        conn,
        app_id,
        HandlerResult(outcome="dry_run", screenshot_path=str(shot)),
        mode="dry_run",
        handler="lever",
        started_at=utcnow(),
    )
    response = client.get(f"/api/applications/{app_id}/screenshot")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")

    shot.unlink()
    assert client.get(f"/api/applications/{app_id}/screenshot").status_code == 404


# ------------------------------------------------------------- answering --


def test_answers_are_stored_and_the_form_can_be_tried_again(client, conn, monkeypatch):
    app_id = _parked(conn, _job(conn))
    tried = {}

    def fake_retry(conn_, application_id, settings, **kwargs):
        tried["id"] = application_id
        return {"job_id": "j1", "application_id": application_id, "outcome": "dry_run"}

    monkeypatch.setattr("jobagent.api.applications.retry_application", fake_retry)

    response = client.post(
        f"/api/applications/{app_id}/answers", json={"answers": {"q_auth": "Yes"}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["needed"] == [] and body["result"] is None
    assert [row["value"] for row in conn.execute("SELECT value FROM answers").fetchall()] == ["Yes"]
    assert tried == {}, "no retry unless it was asked for"

    response = client.post(
        f"/api/applications/{app_id}/answers",
        json={"answers": {"work_authorization": "Yes"}, "retry": True},
    )
    assert response.status_code == 200 and response.json()["result"]["outcome"] == "dry_run"
    assert tried["id"] == app_id
    assert client.app.state.apply_lock.acquire(blocking=False)
    client.app.state.apply_lock.release()


def test_answering_an_application_that_is_not_there(client, conn):
    assert client.post("/api/applications/999/answers", json={"answers": {}}).status_code == 404


def test_a_retry_that_cannot_run_is_a_conflict(client, conn, monkeypatch):
    app_id = _parked(conn, _job(conn))

    def boom(conn_, application_id, settings, **kwargs):
        raise ApplyError("no ready resume for this job")

    monkeypatch.setattr("jobagent.api.applications.retry_application", boom)
    response = client.post(
        f"/api/applications/{app_id}/answers", json={"answers": {"q_auth": "Yes"}, "retry": True}
    )
    assert response.status_code == 409 and "no ready resume" in response.json()["detail"]
    assert client.app.state.apply_lock.acquire(blocking=False), "the lock was let go"
    client.app.state.apply_lock.release()


# -------------------------------------------------------------- approve --


def test_approving_submits_what_was_left_at_the_button(client, conn, monkeypatch):
    app_id = _parked(conn, _job(conn))
    monkeypatch.setattr(
        "jobagent.api.applications.approve_application",
        lambda conn_, application_id, settings, **kw: {
            "application_id": application_id,
            "outcome": "submitted",
        },
    )
    response = client.post(f"/api/applications/{app_id}/approve")
    assert response.status_code == 200 and response.json()["result"]["outcome"] == "submitted"

    assert client.post("/api/applications/999/approve").status_code == 404


def test_approving_one_already_submitted_is_a_conflict(client, conn, monkeypatch):
    app_id = _parked(conn, _job(conn))

    def boom(conn_, application_id, settings, **kwargs):
        raise ApplyError("This application was already submitted.")

    monkeypatch.setattr("jobagent.api.applications.approve_application", boom)
    response = client.post(f"/api/applications/{app_id}/approve")
    assert response.status_code == 409 and "already submitted" in response.json()["detail"]


def test_approving_while_the_browser_is_busy_is_refused(client, conn):
    app_id = _parked(conn, _job(conn))
    client.app.state.apply_lock.acquire()
    try:
        assert client.post(f"/api/applications/{app_id}/approve").status_code == 409
    finally:
        client.app.state.apply_lock.release()


# --------------------------------------------------------------- events --


def test_what_happened_after_is_logged_against_the_application(client, conn):
    app_id = _parked(conn, _job(conn))

    response = client.post(
        f"/api/applications/{app_id}/events",
        json={"kind": "status_change", "to_status": "screening", "note": "recruiter call"},
    )
    assert response.status_code == 201
    events = response.json()
    assert events[0]["to_status"] == "screening" and events[0]["source"] == "manual"
    assert client.get(f"/api/applications/{app_id}").json()["status"] == "screening"

    assert (
        client.post(
            f"/api/applications/{app_id}/events", json={"kind": "status_change"}
        ).status_code
        == 400
    ), "a status change needs a status"
    assert (
        client.post(
            f"/api/applications/{app_id}/events", json={"kind": "note", "note": "thinking"}
        ).status_code
        == 201
    )
    assert (
        client.post(f"/api/applications/{app_id}/events", json={"to_status": "hired"}).status_code
        == 422
    ), "an unknown status is not a status"
    assert client.post("/api/applications/999/events", json={"kind": "note"}).status_code == 404
