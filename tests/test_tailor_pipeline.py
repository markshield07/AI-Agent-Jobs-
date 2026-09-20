"""Tailoring end to end with a fake model and the real validator, gate and renderer."""

from __future__ import annotations

import pytest

from jobagent.answers import set_answers
from jobagent.db.database import utcnow
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.llm.backend import Completion
from jobagent.resume import facts as fact_store
from jobagent.tailor import store
from jobagent.tailor.models import Bullet, CoverLetter, Entry, TailoredResume
from jobagent.tailor.pipeline import TailorError, tailor_job

DESCRIPTION = (
    "We need Python, AWS and Terraform experience for our platform team. "
    "Kubernetes is a plus. You will own billing systems."
)


class FakeCompleter:
    """Hands back plans in order for resume calls, and one letter for letter calls."""

    name = "fake"

    def __init__(self, plans, letters=None):
        self.plans = list(plans)
        self.letters = list(letters or [])
        self.calls = []

    def complete(
        self, *, system, prompt, output, max_tokens=16000, effort=None, cache_system=False
    ):
        self.calls.append({"system": system, "prompt": prompt, "output": output.__name__})
        if output is TailoredResume:
            result = self.plans.pop(0)
        else:
            result = self.letters.pop(0)
        return Completion(result=result, input_tokens=100, output_tokens=10, backend="fake")


@pytest.fixture
def world(conn, settings):
    """A job, a fact base, a never-claim term, contact details and an uploaded resume."""
    role = fact_store.add_fact(
        conn,
        fact_store.Fact(
            kind="role",
            text="Led the billing rewrite at Acme and cut costs 40%",
            detail={"employer": "Acme", "title": "Senior Engineer", "start": "2021", "end": "2024"},
            tags=["python", "aws", "billing"],
        ),
    )
    skills = fact_store.add_keywords(conn, ["Python", "AWS", "Terraform"])
    degree = fact_store.add_fact(
        conn, fact_store.Fact(kind="education", text="BSc Computer Science", detail={})
    )
    fact_store.add_never_claim(conn, "kubernetes")
    set_answers(conn, {"full_name": "Mark Shield", "email": "mark@example.com"})
    conn.execute(
        """INSERT INTO resume_base (filename, stored_path, content_sha, parsed_text, uploaded_at)
           VALUES ('r.txt', '/tmp/r.txt', 'sha', 'Python AWS Terraform billing', ?)""",
        (utcnow(),),
    )
    raw = RawJob(
        url="https://boards.greenhouse.io/beta/jobs/1",
        title="Platform Engineer",
        company="Beta",
        source="greenhouse",
        description=DESCRIPTION,
    )
    job_id = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    return {"job_id": job_id, "role": role, "skills": skills, "degree": degree}


def good_plan(w) -> TailoredResume:
    return TailoredResume(
        summary="Engineer with Python, AWS and Terraform experience on billing systems.",
        skills=w["skills"],
        experience=[
            Entry(
                fact_ids=[w["role"]],
                bullets=[
                    Bullet(fact_id=w["role"], text="Led the billing rewrite, cutting costs 40%")
                ],
            )
        ],
        education=[w["degree"]],
        emphasis="Leads with billing.",
    )


def bad_plan(w) -> TailoredResume:
    plan = good_plan(w)
    plan.experience[0].bullets.append(
        Bullet(fact_id=w["role"], text="Ran the Kubernetes migration across 12 clusters")
    )
    return plan


def thin_plan(w) -> TailoredResume:
    """Valid, but drops keywords the uploaded resume had."""
    return TailoredResume(
        skills=w["skills"][:1],
        experience=[
            Entry(
                fact_ids=[w["role"]],
                bullets=[Bullet(fact_id=w["role"], text="Led the billing rewrite")],
            )
        ],
    )


LETTER = CoverLetter(
    paragraphs=[
        "Dear Beta team, I would like to apply for the Platform Engineer role.",
        "At Acme I led the billing rewrite and cut costs 40%.",
        "Regards, Mark Shield",
    ]
)
BAD_LETTER = CoverLetter(paragraphs=["I run Kubernetes daily.", "Regards"])


def test_clean_plan_becomes_a_ready_variant_with_pdf(conn, settings, world):
    completer = FakeCompleter([good_plan(world)], [LETTER])

    variant = tailor_job(conn, world["job_id"], settings, completer=completer)

    assert variant.status == "ready" and variant.attempts == 1 and not variant.issues
    assert variant.cover_letter is not None
    assert variant.pdf_path and variant.pdf_path.endswith(f"-{variant.id}.pdf")
    with open(variant.pdf_path, "rb") as fh:
        assert fh.read(4) == b"%PDF"
    assert set(variant.keywords) >= {"python", "aws", "terraform", "kubernetes"}
    assert "kubernetes" in variant.keywords_missing
    assert variant.keyword_coverage is not None and variant.base_coverage is not None
    assert variant.keyword_coverage >= variant.base_coverage
    assert variant.tokens_in == 200 and variant.tokens_out == 20
    assert [c["output"] for c in completer.calls] == ["TailoredResume", "CoverLetter"]
    assert completer.calls[0]["system"] == completer.calls[1]["system"]  # cached prefix

    stored = store.get_variant(conn, variant.id)
    assert stored.pdf_path == variant.pdf_path and stored.facts_used == variant.facts_used


def test_invented_claim_is_sent_back_once_then_accepted(conn, settings, world):
    completer = FakeCompleter([bad_plan(world), good_plan(world)], [LETTER])

    variant = tailor_job(conn, world["job_id"], settings, completer=completer)

    assert variant.status == "ready" and variant.attempts == 2
    retry_prompt = completer.calls[1]["prompt"]
    assert "rejected" in retry_prompt.lower() and "kubernetes" in retry_prompt.lower()


def test_two_bad_plans_are_kept_as_rejected(conn, settings, world):
    completer = FakeCompleter([bad_plan(world), bad_plan(world)], [LETTER])

    variant = tailor_job(conn, world["job_id"], settings, completer=completer)

    assert variant.status == "rejected" and variant.attempts == 2
    assert any(i.term and "kubernetes" in i.term.lower() for i in variant.issues)
    assert variant.pdf_path is None and variant.cover_letter is None
    assert len(completer.calls) == 2  # no letter attempted
    assert store.get_variant(conn, variant.id).status == "rejected"
    assert store.latest_ready_variant(conn, world["job_id"]) is None


def test_coverage_gate_sends_a_thin_plan_back(conn, settings, world):
    completer = FakeCompleter([thin_plan(world), thin_plan(world)], [LETTER])

    variant = tailor_job(conn, world["job_id"], settings, completer=completer)

    assert variant.status == "rejected"
    assert variant.issues[0].where == "coverage"
    assert "terraform" in (variant.issues[0].term or "")
    assert "coverage" in completer.calls[1]["prompt"].lower()


def test_bad_cover_letter_is_dropped_not_the_resume(conn, settings, world):
    completer = FakeCompleter([good_plan(world)], [BAD_LETTER, BAD_LETTER])

    variant = tailor_job(conn, world["job_id"], settings, completer=completer)

    assert variant.status == "ready" and variant.cover_letter is None
    assert variant.issues and all("cover letter dropped" in i.message for i in variant.issues)
    assert variant.pdf_path


def test_without_cover_letter(conn, settings, world):
    completer = FakeCompleter([good_plan(world)])
    variant = tailor_job(
        conn, world["job_id"], settings, completer=completer, with_cover_letter=False
    )
    assert variant.status == "ready" and variant.cover_letter is None and len(completer.calls) == 1


def test_errors_before_any_model_call(conn, settings, world):
    completer = FakeCompleter([])
    with pytest.raises(TailorError, match="No job"):
        tailor_job(conn, "nope", settings, completer=completer)

    for fact in fact_store.list_facts(conn):
        fact_store.set_active(conn, fact.id, False)
    with pytest.raises(TailorError, match="No facts"):
        tailor_job(conn, world["job_id"], settings, completer=completer)
    assert completer.calls == []
