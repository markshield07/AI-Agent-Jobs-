"""Variants round-trip through the database with their plan, letter and issues."""

from __future__ import annotations

from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.tailor import store
from jobagent.tailor.models import Bullet, CoverLetter, Entry, Issue, TailoredResume, Variant


def _job(conn) -> str:
    raw = RawJob(url="https://x.example/1", title="Engineer", company="Acme", source="greenhouse")
    return jobs.upsert_jobs(conn, [raw]).new_ids[0]


def _plan() -> TailoredResume:
    return TailoredResume(
        summary="Built things.",
        skills=[3, 4],
        experience=[Entry(fact_ids=[1], bullets=[Bullet(fact_id=1, text="Led the rewrite")])],
        education=[5],
    )


def test_round_trip(conn):
    jid = _job(conn)
    variant = Variant(
        job_id=jid,
        base_id=None,
        content=_plan(),
        cover_letter=CoverLetter(paragraphs=["Hi", "Bye"]),
        keyword_coverage=0.5,
        base_coverage=0.25,
        keywords=["python"],
        keywords_missing=["go"],
        issues=[Issue(where="summary", message="x", term="go")],
        tokens_in=10,
        tokens_out=2,
        attempts=2,
    )
    vid = store.save_variant(conn, variant)
    assert variant.id == vid

    back = store.get_variant(conn, vid)
    assert back is not None
    assert back.content == _plan()
    assert back.cover_letter is not None and back.cover_letter.paragraphs == ["Hi", "Bye"]
    assert back.facts_used == [1, 3, 4, 5]
    assert back.keyword_coverage == 0.5 and back.base_coverage == 0.25
    assert back.keywords == ["python"] and back.keywords_missing == ["go"]
    assert back.issues[0].term == "go" and back.issues[0].where == "summary"
    assert back.tokens_in == 10 and back.attempts == 2 and back.status == "ready"
    assert back.created_at

    store.set_pdf_path(conn, vid, "/tmp/x.pdf")
    assert store.get_variant(conn, vid).pdf_path == "/tmp/x.pdf"


def test_lists_and_latest_ready(conn):
    jid = _job(conn)
    rejected = Variant(job_id=jid, base_id=None, content=_plan(), status="rejected")
    ready = Variant(job_id=jid, base_id=None, content=_plan())
    store.save_variant(conn, rejected)
    ready_id = store.save_variant(conn, ready)
    later_rejected = Variant(job_id=jid, base_id=None, content=_plan(), status="rejected")
    store.save_variant(conn, later_rejected)

    assert [v.status for v in store.list_variants(conn, job_id=jid)] == [
        "rejected",
        "ready",
        "rejected",
    ]
    assert store.latest_ready_variant(conn, jid).id == ready_id
    assert store.latest_ready_variant(conn, "nope") is None
    assert len(store.list_variants(conn, limit=2)) == 2
    assert store.get_variant(conn, 999) is None


def test_summary_shape(conn):
    jid = _job(conn)
    variant = Variant(job_id=jid, base_id=None, content=_plan())
    store.save_variant(conn, variant)
    summary = store.variant_summary(variant)
    assert summary["has_cover_letter"] is False
    assert summary["facts_used"] == [1, 3, 4, 5]
    assert "content" not in summary
