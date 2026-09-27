"""The sign-in routes report status and delete, and never hand out a cookie."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobagent.apply import sessions
from jobagent.main import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_lists_both_sites_with_the_command_to_sign_in(client, settings):
    cookie = {"name": "li_at", "value": "secret-token", "domain": ".linkedin.com", "expires": -1}
    sessions.save_session(settings, "linkedin", {"cookies": [cookie]})

    response = client.get("/api/sessions")
    assert response.status_code == 200
    rows = {r["site"]: r for r in response.json()}
    assert rows["linkedin"]["signed_in"] is True
    assert rows["indeed"]["signed_in"] is False
    assert rows["indeed"]["login_command"] == "jobagent login indeed"
    assert "secret-token" not in response.text


def test_delete_forgets_a_sign_in(client, settings):
    cookie = {"name": "SHOE", "value": "x", "domain": ".indeed.com", "expires": -1}
    sessions.save_session(settings, "indeed", {"cookies": [cookie]})

    assert client.delete("/api/sessions/indeed").json() == {"site": "indeed", "deleted": True}
    assert client.delete("/api/sessions/indeed").json() == {"site": "indeed", "deleted": False}
    assert client.delete("/api/sessions/monster").status_code == 404


def test_each_workday_company_shows_whether_its_sign_in_still_works(client, settings):
    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    cookie = {"name": "PLAY_SESSION", "value": "wd-secret", "domain": host, "expires": -1}
    sessions.save_workday_session(settings, host, {"cookies": [cookie]}, url=posting)
    sessions.mark_workday_signed_out(settings, host, job_id="j1")

    response = client.get("/api/sessions")
    row = next(r for r in response.json() if r["site"] == "workday")
    assert row["host"] == host and row["state"] == "needs_sign_in" and row["waiting"] == 1
    assert row["signed_in"] is False
    assert row["login_command"] == f"jobagent login workday {posting}"
    assert "wd-secret" not in response.text

    assert client.delete(f"/api/sessions/workday/{host}").json()["deleted"] is True
    assert all(r["site"] != "workday" for r in client.get("/api/sessions").json())
    assert client.delete("/api/sessions/workday/..%2Fetc").status_code in (404, 405)
