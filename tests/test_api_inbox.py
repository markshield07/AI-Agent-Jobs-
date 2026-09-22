"""The response-tracking end of the API: poll the mailbox, read the log, and
file a message by hand.

The mailbox itself is stubbed. These tests are about the routes, the lock that
keeps two polls off one mailbox, and the shapes the dashboard reads.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent.api import inbox as routes
from jobagent.apply import store as applications
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.inbox import store
from jobagent.inbox.mailbox import parse_message
from jobagent.inbox.models import Match, Reading
from jobagent.main import create_app

EMAILS = Path(__file__).parent / "fixtures" / "emails"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def conn(client):
    return client.app.state.db.connection()


@pytest.fixture
def application(conn) -> int:
    raw = RawJob(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Backend Engineer",
        company="Acme Robotics",
        source="greenhouse",
    )
    job_id = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    app_id = applications.get_or_create_application(conn, job_id, mode="auto")
    conn.execute(
        "UPDATE applications SET submitted_at = '2026-09-20T12:00:00+00:00' WHERE id = ?",
        (app_id,),
    )
    return app_id


def _message(conn, name: str, application_id: int | None = None, label: str = "unknown") -> int:
    parsed = parse_message((EMAILS / f"{name}.eml").read_bytes())
    match = Match(application_id=application_id, confidence=0.9) if application_id else None
    reading = Reading(label=label, confidence=0.9, reason="said so")  # type: ignore[arg-type]
    return store.record(conn, parsed, match=match, reading=reading)


class FakePoll:
    """Stands in for a mailbox poll and records how it was called."""

    def __init__(self, report):
        self.report, self.calls = report, []

    def __call__(self, conn, settings, **kwargs):
        self.calls.append(kwargs)
        return self.report


# --------------------------------------------------------------- polling --


def test_polling_without_a_mailbox_configured_says_what_is_missing(client):
    response = client.post("/api/inbox/poll?wait=true")
    assert response.status_code == 400
    assert "JOBAGENT_IMAP_HOST" in response.json()["detail"]


def test_a_poll_returns_its_report_when_asked_to_wait(client, monkeypatch):
    from jobagent.inbox.models import PollReport

    report = PollReport(fetched=3, matched=2, unmatched=1, advanced=1)
    poll = FakePoll(report)
    monkeypatch.setattr(routes, "poll_inbox", poll)

    response = client.post(
        "/api/inbox/poll?wait=true", json={"since": "2026-09-01", "limit": 50, "write": False}
    )
    assert response.status_code == 200
    assert response.json()["report"]["matched"] == 2
    assert poll.calls[0] == {"since": "2026-09-01", "limit": 50, "write": False}


def test_the_last_report_is_kept(client, monkeypatch):
    from jobagent.inbox.models import PollReport

    assert client.get("/api/inbox/last").status_code == 404
    monkeypatch.setattr(routes, "poll_inbox", FakePoll(PollReport(fetched=1, matched=1)))
    client.post("/api/inbox/poll?wait=true")
    assert client.get("/api/inbox/last").json()["fetched"] == 1


def test_only_one_poll_runs_at_a_time(client):
    client.app.state.inbox_lock.acquire()
    try:
        assert client.post("/api/inbox/poll").status_code == 409
    finally:
        client.app.state.inbox_lock.release()


def test_a_background_poll_releases_the_lock(client, monkeypatch):
    from jobagent.inbox.models import PollReport

    done = threading.Event()

    def poll(conn, settings, **kwargs):
        done.set()
        return PollReport(fetched=1)

    monkeypatch.setattr(routes, "poll_inbox", poll)
    assert client.post("/api/inbox/poll").status_code == 202
    assert done.wait(5)
    for _ in range(50):
        if not client.app.state.inbox_lock.locked():
            break
    assert not client.app.state.inbox_lock.locked()


# ------------------------------------------------------------- reading --


def test_the_log_is_listed_newest_first(client, conn, application):
    _message(conn, "acknowledgement", application, "acknowledgement")
    _message(conn, "newsletter")
    rows = client.get("/api/inbox").json()
    assert [r["label"] for r in rows] == ["unknown", "acknowledgement"]


def test_the_log_can_be_narrowed_to_what_needs_attention(client, conn, application):
    _message(conn, "acknowledgement", application, "acknowledgement")
    _message(conn, "newsletter")
    unmatched = client.get("/api/inbox?matched=false").json()
    assert len(unmatched) == 1 and unmatched[0]["application_id"] is None
    by_app = client.get(f"/api/inbox?application_id={application}").json()
    assert len(by_app) == 1


def test_an_unknown_label_is_refused(client):
    response = client.get("/api/inbox?label=maybe")
    assert response.status_code == 400 and "Unknown label" in response.json()["detail"]


def test_the_counts_are_what_the_dashboard_tiles_read(client, conn, application):
    _message(conn, "rejection", application, "rejected")
    _message(conn, "newsletter")
    counts = client.get("/api/inbox/counts").json()
    assert counts == {"rejected": 1, "unknown": 1, "unmatched": 1}


# ------------------------------------------------------------ attaching --


def test_a_message_can_be_filed_against_an_application(client, conn, application):
    row_id = _message(conn, "newsletter")
    response = client.post(
        f"/api/inbox/{row_id}/attach",
        json={"application_id": application, "to_status": "screening"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["advanced"] is True
    assert body["application"]["status"] == "screening"
    assert body["message"]["read_by"] == "manual"


def test_filing_against_a_missing_application_is_a_404(client, conn, application):
    row_id = _message(conn, "newsletter")
    response = client.post(f"/api/inbox/{row_id}/attach", json={"application_id": 999})
    assert response.status_code == 404


def test_filing_a_message_that_is_not_there_is_a_404(client, application):
    response = client.post("/api/inbox/404/attach", json={"application_id": application})
    assert response.status_code == 404


def test_an_unknown_status_is_refused(client, conn, application):
    row_id = _message(conn, "newsletter")
    response = client.post(
        f"/api/inbox/{row_id}/attach", json={"application_id": application, "to_status": "hired"}
    )
    assert response.status_code == 422
