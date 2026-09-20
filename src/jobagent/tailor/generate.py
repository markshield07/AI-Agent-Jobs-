"""The prompts and the two model calls behind a tailored resume.

The model is asked for a plan, not a page: which facts to show, in what order,
and how each is phrased, every line citing the fact it came from. The system
prompt carries the rules and the whole fact base and is the same string for
every job and for both calls (resume, then cover letter), so the caller marks
it cacheable and each call pays only for its own posting. When the validator
rejects a plan, its issues go back in the next prompt as a list of things to
fix, with the instruction to change nothing else, so a retry converges instead
of starting over.

Nothing here checks the model's answer; that is `validate`. This module's only
discipline is in what it says: never add, never invent, never rewrite what a
fact does not state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jobagent.llm.backend import Completer, Completion, LLMError
from jobagent.resume.facts import Fact
from jobagent.tailor.models import CoverLetter, Issue, TailoredResume

RESUME_DESCRIPTION_CHARS = 6000
LETTER_DESCRIPTION_CHARS = 4000

FACT_BASE_HEADING = "Fact base"
NEVER_CLAIM_HEADING = "Never claim"
REJECTION_HEADING = (
    "Your previous plan was rejected. Fix every one of these and change nothing else:"
)

_ROLE = """You write the plan for a resume tailored to one job posting, for one specific \
candidate, from their fact base only. The fact base below is everything that is true of this \
candidate; the posting arrives in the next message."""

_RULES = """Rules:
- Choose which facts to show and in what order. Every fact is optional; leave out what does not \
serve this posting.
- Rephrase and tighten each fact into one bullet that cites that fact's id. The bullet may say \
less than the fact; it may never say more.
- Never add an employer, title, date, technology, tool, number, outcome or responsibility that \
the cited fact does not state. A number may appear only if the fact states it. Keep the fact's \
own names for technologies and tools: a rephrasing is checked against the fact word by word.
- The never-claim terms must not appear anywhere, in any section, in any form.
- An experience entry groups role facts that share one employer and one title; a projects entry \
groups project facts the same way. Never mix employers or titles in one entry. The heading is \
rendered from the facts, so cite only the ids.
- Three to five bullets per entry, most relevant first.
- Skills: the skill facts most relevant to the posting first, only skill facts, only ids.
- Education and credentials: only ids of facts of that kind.
- Summary: two or three sentences, or empty. It is checked against every fact, so it may state \
only what the facts state.
- Leave any section empty when nothing fits. An empty section is always better than an invented \
line.
- Set emphasis to one sentence on what this version leads with and why. It is not rendered."""

_RESUME_INSTRUCTION = (
    "Return the plan as the structured output; cite fact ids exactly as listed in the fact base."
)
_LETTER_INSTRUCTION = (
    "Return the letter as the structured output, one paragraph per string, greeting through "
    "sign-off; cite nothing the fact base does not state."
)


# ------------------------------------------------------------------ helpers --


def _text(value: Any) -> str:
    """A prompt-safe string: None becomes empty, anything else is str()-ed and stripped."""
    return "" if value is None else str(value).strip()


def _one_line(value: Any) -> str:
    """Whitespace collapsed, so a fact or a field never breaks its own prompt line."""
    return " ".join(_text(value).split())


def _dedupe_keep_case(terms: Sequence[str]) -> list[str]:
    """Distinct terms in the order given, first spelling kept, blanks dropped."""
    kept: dict[str, str] = {}
    for term in terms:
        cleaned = _one_line(term)
        if cleaned:
            kept.setdefault(cleaned.lower(), cleaned)
    return list(kept.values())


def _term_list(terms: Sequence[str]) -> str:
    cleaned = _dedupe_keep_case(terms)
    return ", ".join(cleaned) if cleaned else "(none)"


def _truncated(description: str, limit: int) -> str:
    text = _text(description)
    if not text:
        return "(no description available)"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _money(value: Any) -> str | None:
    """A salary bound for the prompt, None when absent; a string amount passes through."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.0f}"
    return _text(value) or None


def _salary_line(job: Mapping[str, Any]) -> str | None:
    low, high = _money(job.get("salary_min")), _money(job.get("salary_max"))
    if low and high:
        return f"Salary: {low} - {high}"
    if low:
        return f"Salary: from {low}"
    if high:
        return f"Salary: up to {high}"
    return None


def _location_line(job: Mapping[str, Any]) -> str:
    location = _one_line(job.get("location"))
    remote = job.get("remote")
    if location and remote:
        return f"Location: {location} (remote)"
    if location:
        return f"Location: {location}"
    return "Location: remote" if remote else "Location: not stated"


def _posting_block(job: Mapping[str, Any], description_chars: int, *, full: bool) -> str:
    """The posting as the model sees it; `full` adds location and salary."""
    lines = [
        "Job posting",
        f"Title: {_one_line(job.get('title')) or '(untitled)'}",
        f"Company: {_one_line(job.get('company')) or '(unknown)'}",
    ]
    if full:
        lines.append(_location_line(job))
        salary = _salary_line(job)
        if salary:
            lines.append(salary)
    lines.append("Description:")
    lines.append(_truncated(job.get("description"), description_chars))
    return "\n".join(lines)


def _rejection_block(issues: Sequence[Issue]) -> str:
    lines = [REJECTION_HEADING]
    for issue in issues:
        line = f"- {_one_line(issue.where) or '(unplaced)'}: {_one_line(issue.message)}"
        extras = []
        if issue.term:
            extras.append(f'term "{_one_line(issue.term)}"')
        if issue.fact_id is not None:
            extras.append(f"fact [{issue.fact_id}]")
        if extras:
            line += f" ({', '.join(extras)})"
        lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------- fact lines --


def _dates(detail: Mapping[str, Any]) -> str:
    start, end = _one_line(detail.get("start")), _one_line(detail.get("end"))
    if start and end:
        return f"({start} – {end})"
    if start:
        return f"(from {start})"
    if end:
        return f"(to {end})"
    return ""


def _fact_line(fact: Fact) -> str:
    """`[id] kind · title @ employer (start – end): text | tags: a, b`, absent parts omitted."""
    detail = fact.detail or {}
    head = f"[{fact.id}] {_one_line(fact.kind)}"
    heading = " @ ".join(
        part for part in (_one_line(detail.get("title")), _one_line(detail.get("employer"))) if part
    )
    descriptor = " ".join(part for part in (heading, _dates(detail)) if part)
    if descriptor:
        head += f" · {descriptor}"
    line = f"{head}: {_one_line(fact.text)}"
    tags = _dedupe_keep_case(fact.tags or [])
    if tags:
        line += f" | tags: {', '.join(tags)}"
    return line


def _fact_lines(facts: Sequence[Fact]) -> list[str]:
    # A fact with no id cannot be cited, and a plan may only draw on what it
    # cites, so listing it would only invite an uncited claim. Inactive facts
    # are the candidate's own retractions.
    return [_fact_line(f) for f in facts if f.active and f.id is not None]


# ----------------------------------------------------------------- prompts --


def build_system_prompt(facts: Sequence[Fact], never_claim: Sequence[str]) -> str:
    """The role, the rules, the fact base and the never-claim list.

    Identical for every job and for both calls, so it is the cacheable prefix.
    """
    fact_lines = _fact_lines(facts)
    sections = [
        _ROLE,
        _RULES,
        FACT_BASE_HEADING + "\n" + ("\n".join(fact_lines) if fact_lines else "(no facts on file)"),
        NEVER_CLAIM_HEADING + "\n" + _term_list(never_claim),
    ]
    return "\n\n".join(sections)


def build_resume_prompt(
    job: Mapping[str, Any],
    keywords_supported: Sequence[str],
    keywords_missing: Sequence[str],
    issues: Sequence[Issue] = (),
) -> str:
    """The posting, the two keyword lists, the rejected plan's issues, the ask."""
    sections = [
        _posting_block(job, RESUME_DESCRIPTION_CHARS, full=True),
        "Keywords the fact base supports (lead with these where a fact genuinely covers them): "
        + _term_list(keywords_supported),
        "Keywords the fact base does not support (do not claim these, however tempting): "
        + _term_list(keywords_missing),
    ]
    if issues:
        sections.append(_rejection_block(issues))
    sections.append(_RESUME_INSTRUCTION)
    return "\n\n".join(sections)


def build_cover_letter_prompt(
    job: Mapping[str, Any],
    plan: TailoredResume,
    candidate_name: str | None,
    issues: Sequence[Issue] = (),
) -> str:
    """The posting, what the resume leads with, and the shape of the letter."""
    company = _one_line(job.get("company")) or "the company"
    name = _one_line(candidate_name)
    sign_off = (
        f"Sign off with the candidate's name: {name}."
        if name
        else "The candidate's name is not on file, so sign off without one."
    )
    instructions = (
        "Write the cover letter for this posting: three or four short paragraphs, about 200 "
        f"words in total, addressed to {company} by name. Open with why this role. Then one or "
        "two paragraphs grounded in specific facts from the fact base: cite nothing the fact "
        "base lacks, and the same rules as the resume apply (never add an employer, title, "
        "date, technology, tool, number, outcome or responsibility a fact does not state; the "
        f"never-claim terms must not appear). End with a closing line. {sign_off} No "
        "placeholders such as [Name], [Date] or [Company]. Do not mention salary."
    )
    sections = [
        _posting_block(job, LETTER_DESCRIPTION_CHARS, full=False),
        "Resume plan\n"
        f"Emphasis: {_one_line(plan.emphasis) or '(not stated)'}\n"
        f"Summary: {_one_line(plan.summary) or '(empty)'}",
        instructions,
    ]
    if issues:
        sections.append(_rejection_block(issues))
    sections.append(_LETTER_INSTRUCTION)
    return "\n\n".join(sections)


# ------------------------------------------------------------------- calls --


def _expect(completion: Completion, output: type[TailoredResume] | type[CoverLetter]) -> None:
    # Both backends validate against `output`, so this only fires for a backend
    # that does not; better one clear error here than a mystery in the validator.
    if not isinstance(completion.result, output):
        raise LLMError(
            f"The model returned {type(completion.result).__name__}, not {output.__name__}."
        )


def generate_resume(
    job: Mapping[str, Any],
    facts: Sequence[Fact],
    never_claim: Sequence[str],
    *,
    completer: Completer,
    keywords_supported: Sequence[str],
    keywords_missing: Sequence[str],
    issues: Sequence[Issue] = (),
    effort: str | None = "medium",
) -> tuple[TailoredResume, Completion]:
    """One model call for the plan; `issues` from a rejected attempt shape the retry."""
    completion = completer.complete(
        system=build_system_prompt(facts, never_claim),
        prompt=build_resume_prompt(job, keywords_supported, keywords_missing, issues),
        output=TailoredResume,
        effort=effort,
        cache_system=True,
    )
    _expect(completion, TailoredResume)
    return completion.result, completion  # type: ignore[return-value]


def generate_cover_letter(
    job: Mapping[str, Any],
    facts: Sequence[Fact],
    never_claim: Sequence[str],
    plan: TailoredResume,
    *,
    completer: Completer,
    candidate_name: str | None = None,
    issues: Sequence[Issue] = (),
    effort: str | None = "medium",
) -> tuple[CoverLetter, Completion]:
    """One model call for the letter, under the same system prompt so the cache hits."""
    completion = completer.complete(
        system=build_system_prompt(facts, never_claim),
        prompt=build_cover_letter_prompt(job, plan, candidate_name, issues),
        output=CoverLetter,
        effort=effort,
        cache_system=True,
    )
    _expect(completion, CoverLetter)
    return completion.result, completion  # type: ignore[return-value]
