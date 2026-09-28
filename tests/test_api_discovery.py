from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobagent.discovery import store
from jobagent.discovery.models import RawJob
from jobagent.discovery.pipeline import RunReport
from jobagent.main import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed(client) -> list[str]:
    conn = client.app.state.db.connection()
    result = store.upsert_jobs(
        conn,
        [
            RawJob(url="https://a.example/1", title="Engineer", company="A", source="greenhouse"),
            RawJob(url="https://b.example/2", title="Engineer", company="B", source="indeed"),
        ],
    )
    a, b = result.new_ids
    store.set_rule_score(conn, a, 80, "good")
    store.set_rule_score(conn, b, 20, "poor")
    store.set_status(conn, b, "skipped")
    return [a, b]


def test_criteria_default_then_put(client):
    assert client.get("/api/search-criteria").json()["titles"] == []

    body = {"titles": ["Platform Engineer"], "boards": [{"ats": "greenhouse", "slug": "stripe"}]}
    put = client.put("/api/search-criteria", json=body)
    assert put.status_code == 200
    assert put.json()["boards"] == [{"ats": "greenhouse", "slug": "stripe", "company": None}]
    assert client.get("/api/search-criteria").json()["titles"] == ["Platform Engineer"]


def test_criteria_rejects_out_of_range(client):
    assert client.put("/api/search-criteria", json={"min_score": 500}).status_code == 422
    assert (
        client.put("/api/search-criteria", json={"boards": [{"ats": "x", "slug": "y"}]}).status_code
        == 422
    )


def test_jobs_list_filters_and_counts(client):
    a, b = _seed(client)
    everything = client.get("/api/jobs").json()
    assert [j["id"] for j in everything] == [a, b]  # score desc

    assert [j["id"] for j in client.get("/api/jobs", params={"status": "skipped"}).json()] == [b]
    assert [j["id"] for j in client.get("/api/jobs", params={"min_score": 50}).json()] == [a]
    assert [j["id"] for j in client.get("/api/jobs", params={"source": "indeed"}).json()] == [b]
    assert client.get("/api/jobs", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422

    assert client.get("/api/jobs/counts").json() == {"pending": 1, "skipped": 1}


def test_job_get_and_patch(client):
    a, _ = _seed(client)
    assert client.get(f"/api/jobs/{a}").json()["title"] == "Engineer"
    assert client.get("/api/jobs/nope").status_code == 404

    patched = client.patch(f"/api/jobs/{a}", json={"status": "queued"})
    assert patched.status_code == 200 and patched.json()["status"] == "queued"
    assert client.patch(f"/api/jobs/{a}", json={"status": "applied"}).status_code == 422
    assert client.patch("/api/jobs/nope", json={"status": "queued"}).status_code == 404


def test_discover_wait_returns_report(client, monkeypatch):
    seen = {}

    def fake_run(conn, settings, **kwargs):
        seen.update(kwargs)
        store.finish_run(conn, kwargs["run_id"], found=3)
        return RunReport(run_id=kwargs["run_id"], found=3, new=3)

    monkeypatch.setattr("jobagent.api.discovery.run_discovery", fake_run)

    response = client.post("/api/runs/discover", params={"wait": "true", "classify": "false"})
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["found"] == 3 and seen["classify"] is False

    run = client.get(f"/api/runs/{body['run_id']}").json()
    assert run["found"] == 3 and run["report"]["new"] == 3
    assert client.get("/api/runs").json()[0]["id"] == body["run_id"]
    assert client.get("/api/runs/999").status_code == 404


def test_discover_background_and_one_at_a_time(client, monkeypatch):
    import threading

    release = threading.Event()

    def slow_run(conn, settings, **kwargs):
        release.wait(timeout=5)
        return RunReport(run_id=kwargs["run_id"])

    monkeypatch.setattr("jobagent.api.discovery.run_discovery", slow_run)

    started = client.post("/api/runs/discover")
    assert started.status_code == 202 and started.json()["report"] is None
    assert client.post("/api/runs/discover").status_code == 409

    release.set()
    for _ in range(50):
        if client.app.state.last_report is not None:
            break
        threading.Event().wait(0.02)
    assert client.app.state.last_report["run_id"] == started.json()["run_id"]
    assert client.post("/api/runs/discover", params={"wait": "true"}).status_code == 200
