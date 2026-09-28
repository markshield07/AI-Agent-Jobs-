from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from jobagent.llm.backend import Completion, LLMError
from jobagent.resume.facts import Fact
from jobagent.tailor.generate import (
    LETTER_DESCRIPTION_CHARS,
    REJECTION_HEADING,
    RESUME_DESCRIPTION_CHARS,
    build_cover_letter_prompt,
    build_resume_prompt,
    build_system_prompt,
    generate_cover_letter,
    generate_resume,
)
from jobagent.tailor.models import Bullet, CoverLetter, Entry, Issue, TailoredResume


def _facts() -> list[Fact]:
    return [
        Fact(
            id=1,
            kind="role",
            text="Built billing services in Python on AWS.",
            detail={"employer": "Acme", "title": "Engineer", "start": "2019", "end": "2022"},
            tags=["python", "aws"],
        ),
        Fact(
            id=2,
            kind="role",
            text="Ran the on-call rota.",
            detail={"employer": "Acme", "title": "Engineer", "start": "2019"},
        ),
        Fact(id=3, kind="skill", text="Python", tags=["python"]),
        Fact(id=4, kind="project", text="A CLI for invoices.", detail={"title": "invoicer"}),
        Fact(id=5, kind="education", text="BSc Computer Science", detail={"employer": "Uni"}),
        Fact(id=6, kind="credential", text="AWS Solutions Architect", detail={"end": "2021"}),
        Fact(id=7, kind="summary", text="Retracted claim about Kubernetes.", active=False),
    ]


def _job(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "job01",
        "url": "https://example.com/jobs/1",
        "title": "Platform Engineer",
        "company": "Globex",
        "location": "Berlin, Germany",
        "description": "We need Python and AWS. Terraform is a plus.",
        "salary_min": 90000,
        "salary_max": 120000,
        "remote": 1,
        "score": 80,
        "tier": 1,
        "category": "Backend Engineering",
    }
    row.update(overrides)
    return row


def _plan() -> TailoredResume:
    return TailoredResume(
        summary="Backend engineer with billing experience.",
        skills=[3],
        experience=[
            Entry(fact_ids=[1, 2], bullets=[Bullet(fact_id=1, text="Built billing in Python.")])
        ],
        education=[5],
        emphasis="Leads with billing on AWS.",
    )


def _letter() -> CoverLetter:
    return CoverLetter(paragraphs=["Dear Globex,", "I build billing.", "Regards, Ada"])


@dataclass
class FakeCompleter:
    """Records every call; answers a resume call with the plan and a letter call with the letter."""

    plan: TailoredResume = field(default_factory=_plan)
    letter: CoverLetter = field(default_factory=_letter)
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
        result: BaseModel = self.plan if output is TailoredResume else self.letter
        return Completion(result=result, input_tokens=321, output_tokens=45, backend=self.name)


def _fact_base_lines(system: str) -> list[str]:
    body = system.split("Fact base\n", 1)[1].split("\n\nNever claim", 1)[0]
    return body.splitlines()


# ------------------------------------------------------------ system prompt --


def test_system_prompt_lists_every_active_fact_and_omits_inactive():
    system = build_system_prompt(_facts(), [])
    for fact in _facts():
        if fact.active:
            assert f"[{fact.id}]" in system
            assert fact.text in system
    assert "[7]" not in system
    assert "Retracted claim" not in system
    assert len(_fact_base_lines(system)) == 6


def test_system_prompt_fact_line_shape():
    lines = _fact_base_lines(build_system_prompt(_facts(), []))
    assert lines[0] == (
        "[1] role · Engineer @ Acme (2019 – 2022): Built billing services in Python on AWS."
        " | tags: python, aws"
    )
    assert lines[2] == "[3] skill: Python | tags: python"


def test_system_prompt_missing_detail_parts_render_cleanly():
    system = build_system_prompt(_facts(), [])
    lines = _fact_base_lines(system)
    assert "None" not in system
    assert lines[1] == "[2] role · Engineer @ Acme (from 2019): Ran the on-call rota."
    assert lines[3] == "[4] project · invoicer: A CLI for invoices."
    assert lines[4] == "[5] education · Uni: BSc Computer Science"
    assert lines[5] == "[6] credential · (to 2021): AWS Solutions Architect"
    for line in lines:
        assert " @ (" not in line
        assert "( –" not in line and "– )" not in line
        assert not line.endswith(("|", "·", "@", ":"))


def test_system_prompt_collapses_multiline_fact_text_to_one_line():
    facts = [Fact(id=1, kind="role", text="Built billing\n  services\n\nin Python.")]
    lines = _fact_base_lines(build_system_prompt(facts, []))
    assert lines == ["[1] role: Built billing services in Python."]


def test_system_prompt_skips_facts_without_an_id():
    facts = [Fact(kind="skill", text="Orphan"), Fact(id=9, kind="skill", text="Python")]
    system = build_system_prompt(facts, [])
    assert "Orphan" not in system
    assert "[None]" not in system
    assert "[9] skill: Python" in system


def test_system_prompt_never_claim_terms():
    system = build_system_prompt(_facts(), ["kubernetes", "PhD", " kubernetes ", ""])
    head, tail = system.split("Never claim\n", 1)
    assert tail == "kubernetes, PhD"
    assert "kubernetes" not in head  # only where the rules say never


def test_system_prompt_never_claim_none():
    system = build_system_prompt(_facts(), [])
    assert system.endswith("Never claim\n(none)")


def test_system_prompt_order_and_rules():
    system = build_system_prompt(_facts(), ["x"])
    role = system.index("You write the plan for a resume tailored to one job posting")
    rules = system.index("Rules:")
    facts = system.index("Fact base\n")
    never = system.index("Never claim\n")
    assert role < rules < facts < never
    for phrase in (
        "Never add an employer, title, date, technology, tool, number, outcome or responsibility",
        "A number may appear only if the fact states it",
        "never-claim terms must not appear anywhere",
        "Three to five bullets per entry",
        "only skill facts",
        "two or three sentences, or empty",
        "Set emphasis to one sentence",
    ):
        assert phrase in system


def test_system_prompt_is_the_same_string_for_the_same_input():
    assert build_system_prompt(_facts(), ["x"]) == build_system_prompt(_facts(), ["x"])


def test_system_prompt_with_no_facts():
    system = build_system_prompt([], [])
    assert "Fact base\n(no facts on file)" in system


# ------------------------------------------------------------ resume prompt --


def test_resume_prompt_contains_posting_and_keyword_lists():
    prompt = build_resume_prompt(_job(), ["python", "aws"], ["terraform"])
    assert "Title: Platform Engineer" in prompt
    assert "Company: Globex" in prompt
    assert "Location: Berlin, Germany (remote)" in prompt
    assert "Salary: 90,000 - 120,000" in prompt
    assert "We need Python and AWS. Terraform is a plus." in prompt
    assert (
        "Keywords the fact base supports (lead with these where a fact genuinely covers them): "
        "python, aws"
    ) in prompt
    assert (
        "Keywords the fact base does not support (do not claim these, however tempting): terraform"
    ) in prompt
    assert prompt.endswith(
        "Return the plan as the structured output; cite fact ids exactly as listed in the "
        "fact base."
    )


def test_resume_prompt_empty_keyword_lists_and_missing_fields():
    prompt = build_resume_prompt(
        _job(company=None, location=None, salary_min=None, salary_max=None, remote=None),
        [],
        [],
    )
    assert "None" not in prompt
    assert "Company: (unknown)" in prompt
    assert "Location: not stated" in prompt
    assert "Salary" not in prompt
    assert "covers them): (none)" in prompt
    assert "however tempting): (none)" in prompt


def test_resume_prompt_truncates_long_descriptions_only():
    short = build_resume_prompt(_job(), [], [])
    assert "[truncated]" not in short

    description = "word " * 3000  # 15000 chars
    prompt = build_resume_prompt(_job(description=description), [], [])
    assert "[truncated]" in prompt
    body = prompt.split("Description:\n", 1)[1].split("\n[truncated]", 1)[0]
    assert len(body) <= RESUME_DESCRIPTION_CHARS
    assert len(body) > RESUME_DESCRIPTION_CHARS - 10

    exact = build_resume_prompt(_job(description="x" * RESUME_DESCRIPTION_CHARS), [], [])
    assert "[truncated]" not in exact


def test_resume_prompt_rejection_block_only_with_issues():
    clean = build_resume_prompt(_job(), ["python"], [])
    assert REJECTION_HEADING not in clean
    assert "rejected" not in clean

    issues = [
        Issue(where="summary", message="term not supported by any fact", term="kafka"),
        Issue(
            where="experience[0].bullets[1]",
            message="number not in the fact",
            fact_id=1,
            term="40%",
        ),
        Issue(where="coverage", message="covers 20% of the posting's keywords"),
    ]
    prompt = build_resume_prompt(_job(), ["python"], [], issues)
    assert REJECTION_HEADING in prompt
    block = prompt.split(REJECTION_HEADING, 1)[1]
    assert '- summary: term not supported by any fact (term "kafka")' in block
    assert ('- experience[0].bullets[1]: number not in the fact (term "40%", fact [1])') in block
    assert "- coverage: covers 20% of the posting's keywords\n" in block
    assert "None" not in block
    # The rejection block sits between the keyword lists and the final instruction.
    assert prompt.index("however tempting") < prompt.index(REJECTION_HEADING)
    assert prompt.index(REJECTION_HEADING) < prompt.index("Return the plan")


# ---------------------------------------------------------- generate_resume --


def test_generate_resume_calls_the_completer_with_cache_and_effort():
    completer = FakeCompleter()
    plan, completion = generate_resume(
        _job(),
        _facts(),
        ["kubernetes"],
        completer=completer,
        keywords_supported=["python"],
        keywords_missing=["terraform"],
    )
    assert plan is completer.plan
    assert completion.result is completer.plan
    assert completion.input_tokens == 321
    assert len(completer.calls) == 1
    call = completer.calls[0]
    assert call["cache_system"] is True
    assert call["effort"] == "medium"
    assert call["output"] is TailoredResume
    assert call["system"] == build_system_prompt(_facts(), ["kubernetes"])
    assert call["prompt"] == build_resume_prompt(_job(), ["python"], ["terraform"])


@pytest.mark.parametrize("effort", ["high", None])
def test_generate_resume_passes_effort_through(effort):
    completer = FakeCompleter()
    generate_resume(
        _job(),
        _facts(),
        [],
        completer=completer,
        keywords_supported=[],
        keywords_missing=[],
        effort=effort,
    )
    assert completer.calls[0]["effort"] == effort


def test_generate_resume_puts_issues_in_the_prompt():
    completer = FakeCompleter()
    generate_resume(
        _job(),
        _facts(),
        [],
        completer=completer,
        keywords_supported=[],
        keywords_missing=[],
        issues=[Issue(where="summary", message="on the never-claim list", term="kubernetes")],
    )
    prompt = completer.calls[0]["prompt"]
    assert REJECTION_HEADING in prompt
    assert '- summary: on the never-claim list (term "kubernetes")' in prompt


def test_generate_resume_rejects_a_result_of_the_wrong_type():
    completer = FakeCompleter(plan=_letter())  # type: ignore[arg-type]
    with pytest.raises(LLMError, match="CoverLetter, not TailoredResume"):
        generate_resume(
            _job(), _facts(), [], completer=completer, keywords_supported=[], keywords_missing=[]
        )


# ------------------------------------------------------ cover letter prompt --


def test_cover_letter_prompt_contents():
    prompt = build_cover_letter_prompt(_job(), _plan(), "Ada Lovelace")
    assert "Company: Globex" in prompt
    assert "addressed to Globex by name" in prompt
    assert "Title: Platform Engineer" in prompt
    assert "Emphasis: Leads with billing on AWS." in prompt
    assert "Summary: Backend engineer with billing experience." in prompt
    assert "Ada Lovelace" in prompt
    assert "[Name]" in prompt and "[Date]" in prompt
    assert "No placeholders" in prompt
    assert "Do not mention salary" in prompt
    assert "three or four short paragraphs" in prompt
    assert "200 words" in prompt
    assert "Salary:" not in prompt
    assert "Location:" not in prompt
    assert REJECTION_HEADING not in prompt


def test_cover_letter_prompt_without_a_name_or_emphasis():
    prompt = build_cover_letter_prompt(_job(company=None), TailoredResume(), None)
    assert "None" not in prompt
    assert "name is not on file" in prompt
    assert "Emphasis: (not stated)" in prompt
    assert "Summary: (empty)" in prompt
    assert "addressed to the company by name" in prompt


def test_cover_letter_prompt_truncates_at_its_own_limit():
    prompt = build_cover_letter_prompt(_job(description="y" * 5000), _plan(), None)
    body = prompt.split("Description:\n", 1)[1].split("\n[truncated]", 1)[0]
    assert len(body) == LETTER_DESCRIPTION_CHARS
    assert "[truncated]" in prompt
    assert "[truncated]" not in build_cover_letter_prompt(_job(), _plan(), None)


def test_cover_letter_prompt_rejection_block():
    issues = [Issue(where="cover_letter[1]", message="on the never-claim list", term="kubernetes")]
    prompt = build_cover_letter_prompt(_job(), _plan(), "Ada", issues)
    assert REJECTION_HEADING in prompt
    assert '- cover_letter[1]: on the never-claim list (term "kubernetes")' in prompt
    assert prompt.index(REJECTION_HEADING) < prompt.index("Return the letter")


# ---------------------------------------------------- generate_cover_letter --


def test_generate_cover_letter_shares_the_system_prompt_with_the_resume():
    completer = FakeCompleter()
    plan, _ = generate_resume(
        _job(),
        _facts(),
        ["kubernetes"],
        completer=completer,
        keywords_supported=["python"],
        keywords_missing=[],
    )
    letter, completion = generate_cover_letter(
        _job(), _facts(), ["kubernetes"], plan, completer=completer, candidate_name="Ada"
    )
    assert letter is completer.letter
    assert completion.result is completer.letter
    resume_call, letter_call = completer.calls
    assert letter_call["system"] == resume_call["system"]
    assert letter_call["output"] is CoverLetter
    assert letter_call["cache_system"] is True
    assert letter_call["effort"] == "medium"
    assert letter_call["prompt"] == build_cover_letter_prompt(_job(), plan, "Ada")
    assert "Ada" in letter_call["prompt"]


def test_generate_cover_letter_rejects_a_result_of_the_wrong_type():
    completer = FakeCompleter(letter=_plan())  # type: ignore[arg-type]
    with pytest.raises(LLMError, match="TailoredResume, not CoverLetter"):
        generate_cover_letter(_job(), _facts(), [], _plan(), completer=completer)
