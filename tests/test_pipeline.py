"""The discovery run end to end, with fake sources and a fake model."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobagent.discovery import store
from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.models import RawJob
from jobagent.discovery.pipeline import RunReport, run_discovery
from jobagent.llm.backend import Completion, LLMUnavailable
from jobagent.resume import facts as fact_store

LONG = "Python and AWS and Terraform, Kubernetes, Postgres, Docker, CI and GitHub Actions. " * 8

STRONG = RawJob(
    url="https://boards.greenhouse.io/acme/jobs/1?gh_src=abc",
    title="Senior Software Engineer",
    company="Acme",
    source="greenhouse",
    location="Remote",
    description=LONG,
    remote=True,
    salary_min=150_000,
    salary_max=190_000,
)
# Clears the rules gate (remote, a few keywords) so the model gets to tier it.
WEAK = RawJob(
    url="https://jobs.lever.co/beta/2",
    title="Software Engineer",
    company="Beta",
    source="lever",
    location="Remote",
    description="We need someone who knows Python, Docker and Postgres. " * 20,
)
OFF_TOPIC = RawJob(
    url="https://example.com/sales",
    title="Sales Director",
    company="Gamma",
    source="indeed",
    description="Quota carrying role. " * 30,
)
EXCLUDED = RawJob(
    url="https://example.com/cleared",
    title="Software Engineer (TS/SCI clearance)",
    company="Delta",
    source="indeed",
    description=LONG,
)
SNIPPET = RawJob(
    url="https://example.com/snippet",
    title="Software Engineer",
    company="Epsilon",
    source="linkedin",
    description="Short teaser.",
)
BROKEN = RawJob(url="", title="No url", company="Zeta", source="indeed")


class FakeSource:
    def __init__(self, name: str, jobs: list[RawJob], fail_after: int | None = None) -> None:
        self.name = name
        self.jobs = jobs
        self.fail_after = fail_after

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        for i, job in enumerate(self.jobs):
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("board unreachable")
            yield job


class FakeCompleter:
    """Tiers every id it knows about; anything else is left out, as a model might."""

    name = "fake"

    def __init__(self, tiers: dict[str, int]) -> None:
        self.tiers = tiers
        self.calls: list[dict] = []

    def complete(
        self, *, system, prompt, output, max_tokens=16000, effort=None, cache_system=False
    ):
        self.calls.append({"system": system, "prompt": prompt, "effort": effort})
        items = [
            {
                "job_id": jid,
                "tier": tier,
                "fit_score": 90 if tier == 1 else 40,
                "category": "Backend Engineering",
                "reason": "fake",
            }
            for jid, tier in self.tiers.items()
            if jid in prompt
        ]
        return Completion(
            result=output.model_validate({"items": items}),
            input_tokens=1000,
            output_tokens=100,
            backend="fake",
        )


@pytest.fixture
def criteria() -> SearchCriteria:
    return SearchCriteria(
        titles=["Software Engineer"],
        keywords=["python", "aws", "terraform", "kubernetes", "postgres", "docker"],
        exclude_keywords=["clearance"],
        salary_min=120_000,
        min_score=60,
        max_tier=2,
    )


@pytest.fixture
def profile(conn):
    fact_store.add_keywords(conn, ["Python", "AWS", "Terraform"])


def _ids(*jobs: RawJob) -> list[str]:
    return [store.job_id(j.url, j.title, j.company) for j in jobs]


def test_full_run(conn, settings, criteria, profile, monkeypatch):
    monkeypatch.setattr(
        "jobagent.discovery.pipeline.enrich_description",
        lambda url, **kw: LONG if url.endswith("/snippet") else None,
    )
    strong_id, weak_id, snippet_id = _ids(STRONG, WEAK, SNIPPET)
    completer = FakeCompleter({strong_id: 1, weak_id: 3, snippet_id: 2})
    sources = [
        FakeSource("one", [STRONG, WEAK, OFF_TOPIC, BROKEN]),
        FakeSource("two", [EXCLUDED, SNIPPET, STRONG], fail_after=2),
    ]

    report = run_discovery(
        conn, settings, criteria=criteria, sources=sources, completer=completer, client=None
    )

    assert isinstance(report, RunReport)
    assert report.found == 6 and report.new == 5 and report.rejected == 1
    assert report.duplicates == 0  # the repeat STRONG was never yielded: source two died first
    assert report.per_source == {"one": 4, "two": 2}
    assert "two" in report.source_errors and "ConnectionError" in report.source_errors["two"]

    assert report.enriched == 1
    assert store.get_job(conn, snippet_id)["description"] == LONG

    assert report.scored == 5
    assert report.passed_rules == 3
    assert report.classified == 3 and report.queued == 2 and report.unclassified == 0
    assert report.skipped == 3  # off-topic, excluded, and the tier-3 one
    assert report.input_tokens == 1000 and report.output_tokens == 100

    by_id = {j["id"]: j for j in store.list_jobs(conn, limit=50)}
    assert by_id[strong_id]["status"] == "queued" and by_id[strong_id]["tier"] == 1
    assert by_id[snippet_id]["status"] == "queued" and by_id[snippet_id]["tier"] == 2
    assert by_id[weak_id]["status"] == "skipped" and by_id[weak_id]["tier"] == 3
    excluded = by_id[_ids(EXCLUDED)[0]]
    assert excluded["status"] == "skipped" and excluded["score"] == 0
    assert "clearance" in excluded["score_reason"]
    assert by_id[_ids(OFF_TOPIC)[0]]["status"] == "skipped"

    # The profile went into the system prompt, so it is the cacheable prefix.
    assert "Terraform" in completer.calls[0]["system"]

    run = store.list_runs(conn)[0]
    assert run["id"] == report.run_id and run["ended_at"] is not None
    assert run["found"] == 6 and run["scored"] == 5 and run["tokens_in"] == 1000


def test_no_model_leaves_survivors_pending(conn, settings, criteria, profile, monkeypatch):
    def unavailable(_settings):
        raise LLMUnavailable("nothing configured")

    monkeypatch.setattr("jobagent.discovery.pipeline.resolve_backend", unavailable)
    report = run_discovery(
        conn, settings, criteria=criteria, sources=[FakeSource("one", [STRONG])], enrich=False
    )

    assert report.passed_rules == 1 and report.classified == 0 and report.unclassified == 1
    assert store.get_job(conn, _ids(STRONG)[0])["status"] == "pending"
    assert report.notes and "nothing configured" in report.notes[0]


def test_next_run_picks_up_what_was_left(conn, settings, criteria, profile):
    strong_id = _ids(STRONG)[0]
    first = run_discovery(
        conn, settings, criteria=criteria, sources=[FakeSource("one", [STRONG])], classify=False
    )
    assert first.passed_rules == 1 and first.classified == 0

    second = run_discovery(
        conn,
        settings,
        criteria=criteria,
        sources=[FakeSource("one", [STRONG])],
        completer=FakeCompleter({strong_id: 1}),
        enrich=False,
    )
    assert second.new == 0 and second.duplicates == 1
    assert second.scored == 0  # already scored last time
    assert second.classified == 1 and second.queued == 1
    assert store.get_job(conn, strong_id)["status"] == "queued"


def test_run_is_closed_even_when_a_stage_raises(conn, settings, criteria, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr("jobagent.discovery.pipeline.score_rules", boom)
    with pytest.raises(RuntimeError):
        run_discovery(
            conn, settings, criteria=criteria, sources=[FakeSource("one", [STRONG])], enrich=False
        )
    assert store.list_runs(conn)[0]["ended_at"] is not None


def test_summary_line(conn, settings, criteria, profile):
    report = run_discovery(
        conn,
        settings,
        criteria=criteria,
        sources=[FakeSource("one", [STRONG, OFF_TOPIC])],
        completer=FakeCompleter({_ids(STRONG)[0]: 1}),
        enrich=False,
    )
    line = report.summary()
    assert line.startswith(f"Run {report.run_id}: found 2 (2 new, 0 seen before)")
    assert "rules passed 1 of 2" in line and "queued 1" in line and "Tokens:" in line
