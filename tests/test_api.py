from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobagent.main import create_app
from jobagent.resume.parser import ParsedContact, ParsedFact, ParsedResume

STUB_PARSE = ParsedResume(
    contact=ParsedContact(name="Mark Shield", email="mark@example.com"),
    facts=[
        ParsedFact(kind="role", text="Led the billing rewrite", employer="Acme", tags=["python"]),
        ParsedFact(kind="skill", text="Python", tags=["python"]),
    ],
)


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(
        "jobagent.resume.intake.resume_parser.parse_resume",
        lambda text, **kwargs: STUB_PARSE,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_no_resume_yet(client):
    assert client.get("/api/resume").status_code == 404


def test_upload_creates_facts_and_contact_answers(client):
    response = client.post(
        "/api/resume", files={"file": ("resume.txt", b"Mark Shield\nEngineer", "text/plain")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["facts_created"] == 2
    assert body["already_uploaded"] is False

    facts = client.get("/api/facts").json()
    assert {f["text"] for f in facts} == {"Led the billing rewrite", "Python"}
    assert all(f["source"] == "parsed" for f in facts)

    answers = {a["key"]: a["value"] for a in client.get("/api/answers").json()}
    assert answers["full_name"] == "Mark Shield"

    assert client.get("/api/resume").json()["filename"] == "resume.txt"


def test_reupload_does_not_parse_again(client):
    payload = {"file": ("resume.txt", b"Mark Shield\nEngineer", "text/plain")}
    client.post("/api/resume", files=payload)
    second = client.post("/api/resume", files=payload).json()

    assert second["already_uploaded"] is True
    assert second["facts_created"] == 0
    assert len(client.get("/api/facts").json()) == 2, "facts are not duplicated"


def test_upload_rejects_an_unreadable_file(client):
    response = client.post(
        "/api/resume", files={"file": ("resume.pages", b"binary", "application/octet-stream")}
    )
    assert response.status_code == 400
    assert "expected one of" in response.json()["detail"]


def test_upload_rejects_an_empty_file(client):
    response = client.post("/api/resume", files={"file": ("resume.txt", b"", "text/plain")})
    assert response.status_code == 400


def test_add_keywords(client):
    response = client.post("/api/facts/keywords", json={"keywords": ["Terraform", "gRPC"]})
    assert response.status_code == 201
    created = response.json()
    assert {f["text"] for f in created} == {"Terraform", "gRPC"}
    assert all(f["source"] == "user_added" for f in created)

    again = client.post("/api/facts/keywords", json={"keywords": ["terraform"]})
    assert again.json() == [], "an existing skill is skipped rather than duplicated"


def test_keywords_on_the_never_claim_list_are_refused(client):
    client.post("/api/never-claim", json={"term": "Kubernetes", "note": "never used it"})

    response = client.post("/api/facts/keywords", json={"keywords": ["kubernetes", "Kafka"]})
    assert response.status_code == 409
    assert "Kubernetes" in response.json()["detail"]
    assert client.get("/api/facts").json() == [], "nothing is added when one term is blocked"


def test_a_fact_naming_a_never_claim_term_is_refused(client):
    client.post("/api/never-claim", json={"term": "Kubernetes"})
    response = client.post("/api/facts", json={"kind": "role", "text": "Ran a Kubernetes cluster"})
    assert response.status_code == 409


def test_never_claim_crud(client):
    client.post("/api/never-claim", json={"term": "Kubernetes"})
    assert [e["term"] for e in client.get("/api/never-claim").json()] == ["Kubernetes"]

    assert client.delete("/api/never-claim/Kubernetes").status_code == 204
    assert client.get("/api/never-claim").json() == []
    assert client.delete("/api/never-claim/Kubernetes").status_code == 404


def test_deactivating_a_fact_removes_it_from_the_default_list(client):
    fact = client.post("/api/facts", json={"kind": "skill", "text": "COBOL"}).json()

    patched = client.patch(f"/api/facts/{fact['id']}", json={"active": False}).json()
    assert patched["active"] is False
    assert client.get("/api/facts").json() == []
    assert len(client.get("/api/facts", params={"include_inactive": True}).json()) == 1


def test_patching_a_missing_fact_is_404(client):
    assert client.patch("/api/facts/999", json={"active": False}).status_code == 404
