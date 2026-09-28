"""Turn resume text into individually addressable facts.

This is the only place the model is allowed to read a resume, and it is an
extraction job, not a writing job: every fact it returns must be traceable to
words already on the page. Facts are what the tailoring stage draws from in
phase 3, so an invention here becomes a lie on a real application later.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jobagent.llm.backend import Completer
from jobagent.resume.facts import Fact

FactKind = Literal["role", "project", "skill", "education", "credential", "summary"]

_SYSTEM = """You extract structured facts from a resume. You are not writing or \
improving anything.

Rules:
- Every fact must be supported by text already in the resume. Never add an \
employer, date, metric, technology or achievement that is not written there.
- Split the resume into the smallest useful claims: one fact per role, per \
project, per distinct skill, per degree or certification.
- For a role or project, `text` is a one-line description of what the person \
did, phrased as it appears in the resume rather than rewritten.
- `tags` are lowercase keywords a job posting might match on: technologies, \
domains, methodologies. Take them from the resume's own words.
- If a field is not stated in the resume, leave it null. Do not guess dates.
- One `summary` fact at most, and only if the resume actually has a summary or \
objective section."""


class ParsedFact(BaseModel):
    kind: FactKind
    text: str = Field(description="The claim, in the resume's own words.")
    employer: str | None = Field(default=None, description="Company or institution.")
    title: str | None = Field(default=None, description="Job or project title.")
    start: str | None = Field(default=None, description="As written, e.g. 'Mar 2021'.")
    end: str | None = Field(default=None, description="As written, or 'Present'.")
    tags: list[str] = Field(default_factory=list)


class ParsedContact(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    contact: ParsedContact
    facts: list[ParsedFact]


def parse_resume(text: str, *, completer: Completer) -> ParsedResume:
    """Extract contact details and facts from resume text."""
    # No cached prefix here: the system prompt is short and the resume differs per
    # upload. Caching belongs on the fact base in phase 3, where one prefix is
    # reused across every job.
    completion = completer.complete(
        system=_SYSTEM,
        prompt=f"<resume>\n{text}\n</resume>",
        output=ParsedResume,
    )
    return completion.result  # type: ignore[return-value]


def to_facts(parsed: ParsedResume, base_id: int | None = None) -> list[Fact]:
    """Map the model's output onto fact-base rows."""
    facts: list[Fact] = []
    for item in parsed.facts:
        detail = {
            key: value
            for key, value in (
                ("employer", item.employer),
                ("title", item.title),
                ("start", item.start),
                ("end", item.end),
            )
            if value
        }
        facts.append(
            Fact(
                kind=item.kind,
                text=item.text,
                detail=detail,
                tags=item.tags,
                source="parsed",
                base_id=base_id,
            )
        )
    return facts


def contact_answers(contact: ParsedContact) -> dict[str, str]:
    """Contact details as answer-bank entries, for form filling in phase 4."""
    answers = {
        key: value
        for key, value in (
            ("full_name", contact.name),
            ("email", contact.email),
            ("phone", contact.phone),
            ("location", contact.location),
        )
        if value
    }
    if contact.links:
        answers["links"] = ", ".join(contact.links)
    return answers
