from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jobagent.discovery.scoring.classify import (
    ClassificationBatch,
    ClassifiedJob,
    build_batch_prompt,
    build_system_prompt,
    classify_jobs,
)
from jobagent.llm.backend import Completion, LLMError

PROFILE = "Summary:\n- Backend engineer, eight years of Python and Postgres.\n\nSkills: Python, SQL"


def _job(i: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"job{i:02d}",
        "title": f"Software Engineer {i}",
        "company": f"Company {i}",
        "location": "Berlin, Germany",
        "description": f"We build things with Python. Posting number {i}.",
        "salary_min": None,
        "salary_max": None,
        "remote": None,
    }
    row.update(overrides)
    return row


def _item(jid: str, tier: int = 2, fit: int = 70) -> ClassifiedJob:
    return ClassifiedJob(
        job_id=jid,
        tier=tier,
        fit_score=fit,
        category="Backend Engineering",
        reason=f"Posting {jid} wants Python, which the profile lists.",
    )


def _batch(*ids: str) -> ClassificationBatch:
    return ClassificationBatch(items=[_item(jid) for jid in ids])


def _validation_error() -> ValidationError:
    """A real pydantic error, as the API backend raises when the model emits tier=5."""
    try:
        bad = {"job_id": "x", "tier": 5, "fit_score": 101, "category": "c", "reason": "r"}
        ClassificationBatch.model_validate({"items": [bad]})
    except ValidationError as exc:
        return exc
    raise AssertionError("schema accepted an out-of-range tier")


@dataclass
class FakeCompleter:
    """Hands back canned batches in order; an Exception entry is raised instead."""

    responses: list[ClassificationBatch | Exception] = field(default_factory=list)
    tokens: tuple[int, int] = (100, 20)
    calls: list[dict[str, Any]] = field(default_factory=list)
    name: str = "fake"

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        output: type[BaseModel],
        max_tokens: int = 16000,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "output": output,
                "max_tokens": max_tokens,
                "effort": effort,
                "cache_system": cache_system,
            }
        )
        if not self.responses:
            raise AssertionError("FakeCompleter called more times than responses were given")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Completion(
            result=response,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            backend=self.name,
        )


# ------------------------------------------------------------------ batching --


def test_empty_input_makes_no_call():
    completer = FakeCompleter()
    result = classify_jobs([], profile=PROFILE, completer=completer)
    assert result.items == []
    assert result.unclassified == []
    assert result.input_tokens == 0 and result.output_tokens == 0
    assert completer.calls == []


def test_seventeen_jobs_make_three_batches_of_eight():
    jobs = [_job(i) for i in range(17)]
    ids = [j["id"] for j in jobs]
    completer = FakeCompleter(
        responses=[_batch(*ids[0:8]), _batch(*ids[8:16]), _batch(*ids[16:17])]
    )

    result = classify_jobs(jobs, profile=PROFILE, completer=completer, batch_size=8)

    assert len(completer.calls) == 3
    for call, expected in zip(completer.calls, (ids[0:8], ids[8:16], ids[16:17]), strict=True):
        for jid in expected:
            assert f"job_id: {jid}" in call["prompt"]
        others = set(ids) - set(expected)
        assert not any(f"job_id: {jid}" in call["prompt"] for jid in others)
    assert [item.job_id for item in result.items] == ids
    assert result.unclassified == []


def test_cache_system_effort_and_output_type_pass_through():
    jobs = [_job(1), _job(2)]
    completer = FakeCompleter(responses=[_batch("job01", "job02")])

    classify_jobs(jobs, profile=PROFILE, completer=completer, effort="high")

    (call,) = completer.calls
    assert call["cache_system"] is True
    assert call["effort"] == "high"
    assert call["output"] is ClassificationBatch


def test_system_prompt_identical_across_batches():
    jobs = [_job(i) for i in range(4)]
    completer = FakeCompleter(responses=[_batch("job00", "job01"), _batch("job02", "job03")])

    classify_jobs(jobs, profile=PROFILE, completer=completer, batch_size=2)

    systems = {call["system"] for call in completer.calls}
    assert len(systems) == 1
    assert PROFILE in systems.pop()


def test_duplicate_input_ids_are_sent_and_returned_once():
    jobs = [_job(1), _job(1, title="Same id again"), _job(2)]
    completer = FakeCompleter(responses=[_batch("job01", "job02")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    (call,) = completer.calls
    assert call["prompt"].count("job_id: job01") == 1
    assert "Same id again" not in call["prompt"]
    assert "Classify these 2 postings" in call["prompt"]
    assert [item.job_id for item in result.items] == ["job01", "job02"]
    assert result.unclassified == []


# ------------------------------------------------------------- reconciliation --


def test_missing_id_goes_to_unclassified():
    jobs = [_job(1), _job(2), _job(3)]
    completer = FakeCompleter(responses=[_batch("job01", "job03")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    assert [item.job_id for item in result.items] == ["job01", "job03"]
    assert result.unclassified == ["job02"]


def test_unknown_id_is_ignored():
    jobs = [_job(1)]
    completer = FakeCompleter(responses=[_batch("job01", "not-a-job", "job99")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    assert [item.job_id for item in result.items] == ["job01"]
    assert result.unclassified == []


def test_duplicate_id_first_wins():
    jobs = [_job(1)]
    first = _item("job01", tier=1, fit=95)
    second = _item("job01", tier=4, fit=5)
    completer = FakeCompleter(responses=[ClassificationBatch(items=[first, second])])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    (item,) = result.items
    assert item.tier == 1 and item.fit_score == 95


def test_whitespace_around_returned_id_is_forgiven():
    jobs = [_job(1)]
    completer = FakeCompleter(responses=[_batch(" job01\n")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    assert [item.job_id for item in result.items] == ["job01"]
    assert result.unclassified == []


def test_whitespace_around_input_id_is_stripped_before_sending():
    jobs = [_job(1, id=" job01 ")]
    completer = FakeCompleter(responses=[_batch("job01")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    prompt = completer.calls[0]["prompt"]
    assert "job_id: job01\n" in prompt
    assert "no others: job01\n" in prompt
    assert [item.job_id for item in result.items] == ["job01"]
    assert result.unclassified == []


def test_out_of_range_values_are_clamped():
    # model_construct skips validation, standing in for a backend that does not
    # validate against the schema.
    too_high = ClassifiedJob.model_construct(
        job_id="job01", tier=7, fit_score=140, category="Backend Engineering", reason="r"
    )
    too_low = ClassifiedJob.model_construct(
        job_id="job02", tier=0, fit_score=-5, category="Backend Engineering", reason="r"
    )
    completer = FakeCompleter(responses=[ClassificationBatch(items=[too_high, too_low])])

    result = classify_jobs([_job(1), _job(2)], profile=PROFILE, completer=completer)

    by_id = {item.job_id: item for item in result.items}
    assert (by_id["job01"].tier, by_id["job01"].fit_score) == (4, 100)
    assert (by_id["job02"].tier, by_id["job02"].fit_score) == (1, 0)


def test_schema_rejects_tier_and_score_out_of_range():
    with pytest.raises(ValidationError):
        _item("job01", tier=5)
    with pytest.raises(ValidationError):
        _item("job01", fit=101)


def test_result_order_follows_input_not_response():
    jobs = [_job(1), _job(2), _job(3)]
    completer = FakeCompleter(responses=[_batch("job03", "job01", "job02")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    assert [item.job_id for item in result.items] == ["job01", "job02", "job03"]


# ----------------------------------------------------------- failure isolation --


def test_failing_batch_is_isolated_and_others_still_run():
    jobs = [_job(i) for i in range(6)]
    completer = FakeCompleter(
        responses=[
            _batch("job00", "job01"),
            LLMError("model fell over"),
            _batch("job04", "job05"),
        ]
    )

    result = classify_jobs(jobs, profile=PROFILE, completer=completer, batch_size=2)

    assert len(completer.calls) == 3
    assert [item.job_id for item in result.items] == ["job00", "job01", "job04", "job05"]
    assert result.unclassified == ["job02", "job03"]
    # The failed batch reported no usage, so only two batches count.
    assert result.input_tokens == 200 and result.output_tokens == 40


def test_validation_error_batch_is_isolated_and_others_still_run():
    # The API backend parses the model's JSON with pydantic after the call and
    # the SDK strips numeric bounds from the schema it sends, so an out-of-range
    # tier reaches classify_jobs as a ValidationError, not an LLMError.
    jobs = [_job(i) for i in range(6)]
    completer = FakeCompleter(
        responses=[
            _batch("job00", "job01"),
            _validation_error(),
            _batch("job04", "job05"),
        ]
    )

    result = classify_jobs(jobs, profile=PROFILE, completer=completer, batch_size=2)

    assert len(completer.calls) == 3
    assert [item.job_id for item in result.items] == ["job00", "job01", "job04", "job05"]
    assert result.unclassified == ["job02", "job03"]
    assert result.input_tokens == 200 and result.output_tokens == 40


def test_non_llm_errors_propagate():
    completer = FakeCompleter(responses=[RuntimeError("bug in the caller")])
    with pytest.raises(RuntimeError):
        classify_jobs([_job(1)], profile=PROFILE, completer=completer)


def test_tokens_accumulate_across_batches():
    jobs = [_job(i) for i in range(5)]
    completer = FakeCompleter(
        responses=[_batch("job00", "job01"), _batch("job02", "job03"), _batch("job04")],
        tokens=(1000, 50),
    )

    result = classify_jobs(jobs, profile=PROFILE, completer=completer, batch_size=2)

    assert result.input_tokens == 3000
    assert result.output_tokens == 150


# ------------------------------------------------------------------- prompts --


def test_system_prompt_carries_profile_under_heading():
    system = build_system_prompt(PROFILE)
    assert "Candidate profile:" in system
    assert system.index("Candidate profile:") < system.index(PROFILE)
    assert "tier 4" in system.lower()
    assert "fit_score" in system


def test_blank_profile_is_labelled_not_empty():
    assert build_system_prompt("   ").endswith("(no resume on file)")


def test_non_string_profile_is_tolerated():
    assert build_system_prompt(None).endswith("(no resume on file)")  # type: ignore[arg-type]
    assert build_system_prompt(42).endswith("42")  # type: ignore[arg-type]


def test_batch_prompt_lists_every_job_and_its_fields():
    jobs = [
        _job(1, salary_min=120000, salary_max=150000, remote=True),
        _job(2, location=None, remote=True, salary_min=None, salary_max=90000),
        _job(3, location=None, remote=None),
    ]
    prompt = build_batch_prompt(jobs)

    for job in jobs:
        assert f"job_id: {job['id']}" in prompt
        assert f"Title: {job['title']}" in prompt
        assert f"Company: {job['company']}" in prompt
    assert "exactly these ids and no others: job01, job02, job03" in prompt
    assert "Location: Berlin, Germany (remote)" in prompt
    assert "Salary: 120,000 - 150,000" in prompt
    assert "Location: remote" in prompt
    assert "Salary: up to 90,000" in prompt
    assert "Location: not stated" in prompt


def test_batch_prompt_omits_salary_when_absent():
    prompt = build_batch_prompt([_job(1)])
    assert "Salary" not in prompt


def test_non_numeric_salary_does_not_abort_the_run():
    jobs = [
        _job(1, salary_min="120000", salary_max="150k"),
        _job(2, salary_min=95000.0, salary_max=None),
        _job(3, salary_min="", salary_max=" "),
    ]
    completer = FakeCompleter(responses=[_batch("job01", "job02", "job03")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    prompt = completer.calls[0]["prompt"]
    assert "Salary: 120000 - 150k" in prompt
    assert "Salary: from 95,000" in prompt
    assert prompt.count("Salary:") == 2
    assert [item.job_id for item in result.items] == ["job01", "job02", "job03"]


def test_non_string_description_and_fields_are_tolerated():
    prompt = build_batch_prompt([_job(1, description=12345, title=7, company=None, location=0)])
    assert "Description:\n12345" in prompt
    assert "Title: 7" in prompt
    assert "Company: (unknown)" in prompt
    assert "Location: 0" in prompt


def test_description_is_truncated():
    long_text = "x" * 100 + "TAIL-MARKER"
    completer = FakeCompleter(responses=[_batch("job01")])

    classify_jobs(
        [_job(1, description=long_text)],
        profile=PROFILE,
        completer=completer,
        max_description_chars=100,
    )

    prompt = completer.calls[0]["prompt"]
    assert "x" * 100 in prompt
    assert "TAIL-MARKER" not in prompt
    assert "[truncated]" in prompt


def test_short_description_is_not_marked_truncated():
    prompt = build_batch_prompt([_job(1, description="short")], max_description_chars=100)
    assert "short" in prompt
    assert "[truncated]" not in prompt


def test_missing_description_is_labelled():
    prompt = build_batch_prompt([_job(1, description=None)])
    assert "(no description available)" in prompt


def test_jobs_without_id_are_skipped_not_sent():
    jobs = [_job(1), {"title": "No id", "company": "X"}, _job(2, id="  ", title="Blank id")]
    completer = FakeCompleter(responses=[_batch("job01")])

    result = classify_jobs(jobs, profile=PROFILE, completer=completer)

    assert len(completer.calls) == 1
    assert "No id" not in completer.calls[0]["prompt"]
    assert "Blank id" not in completer.calls[0]["prompt"]
    assert [item.job_id for item in result.items] == ["job01"]
    assert result.unclassified == []


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError):
        classify_jobs([_job(1)], profile=PROFILE, completer=FakeCompleter(), batch_size=0)


@pytest.mark.parametrize("limit", [0, -1])
def test_max_description_chars_must_be_positive(limit: int):
    completer = FakeCompleter()
    with pytest.raises(ValueError):
        classify_jobs([_job(1)], profile=PROFILE, completer=completer, max_description_chars=limit)
    assert completer.calls == []
    with pytest.raises(ValueError):
        build_batch_prompt([_job(1)], max_description_chars=limit)
