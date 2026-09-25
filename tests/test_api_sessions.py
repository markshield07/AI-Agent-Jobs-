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
