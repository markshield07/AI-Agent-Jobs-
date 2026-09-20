from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.llm.backend import LLMUnavailable
from jobagent.main import create_app
from jobagent.tailor import store
from jobagent.tailor.models import Bullet, CoverLetter, Entry, TailoredResume, Variant
from jobagent.tailor.pipeline import TailorError


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _jobs(conn) -> list[str]:
    """Two stored jobs, so variants have something to reference."""
    raws = [
        RawJob(url="https://a.example/1", title="A", company="A", source="indeed"),
        RawJob(url="https://b.example/2", title="B", company="B", source="indeed"),
    ]
    return jobs.upsert_jobs(conn, raws).new_ids


def _variant(job_id, pdf=None, letter=True) -> Variant:
    plan = TailoredResume(
        skills=[1], experience=[Entry(fact_ids=[2], bullets=[Bullet(fact_id=2, text="Did it")])]
    )
    return Variant(
        job_id=job_id,
        base_id=None,
        content=plan,
        cover_letter=CoverLetter(paragraphs=["Hello", "Bye"]) if letter else None,
        keyword_coverage=0.5,
        pdf_path=pdf,
    )


def test_tailor_returns_the_variant(client, monkeypatch, tmp_path):
    seen = {}

    def fake(conn, job_id, settings, **kwargs):
        seen.update(kwargs, job_id=job_id)
        variant = _variant(job_id)
        store.save_variant(conn, variant)
        return variant

    monkeypatch.setattr("jobagent.api.tailoring.tailor_job", fake)
    job_a, _ = _jobs(client.app.state.db.connection())
    response = client.post(f"/api/jobs/{job_a}/tailor", params={"cover_letter": "false"})
    assert response.status_code == 201
    body = response.json()
    assert body["job_id"] == job_a and body["content"]["skills"] == [1]
    assert body["cover_letter"] == ["Hello", "Bye"]
    assert seen["with_cover_letter"] is False


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TailorError("No job x."), 404),
        (TailorError("No facts on file."), 409),
        (LLMUnavailable("nothing configured"), 503),
    ],
)
def test_tailor_error_mapping(client, monkeypatch, error, status):
    def fake(conn, job_id, settings, **kwargs):
        raise error

    monkeypatch.setattr("jobagent.api.tailoring.tailor_job", fake)
    assert client.post("/api/jobs/job1/tailor").status_code == status


def test_variant_reads(client, tmp_path):
    conn = client.app.state.db.connection()
    job_a, job_b = _jobs(conn)
    pdf = tmp_path / "v.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    vid = store.save_variant(conn, _variant(job_a, pdf=str(pdf)))
    store.save_variant(conn, _variant(job_b, letter=False))

    assert [v["job_id"] for v in client.get("/api/variants").json()] == [job_b, job_a]
    assert [v["id"] for v in client.get(f"/api/jobs/{job_a}/variants").json()] == [vid]
    assert client.get("/api/jobs/none/variants").json() == []

    detail = client.get(f"/api/variants/{vid}").json()
    assert detail["content"]["experience"][0]["bullets"][0]["text"] == "Did it"
    assert client.get("/api/variants/999").status_code == 404

    pdf_response = client.get(f"/api/variants/{vid}/pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF")
    assert client.get(f"/api/variants/{vid + 1}/pdf").status_code == 404

    letter = client.get(f"/api/variants/{vid}/cover-letter")
    assert letter.status_code == 200 and letter.text == "Hello\n\nBye"
    assert client.get(f"/api/variants/{vid + 1}/cover-letter").status_code == 404
