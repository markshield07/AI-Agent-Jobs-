"""What a tailored resume is, as data.

The model never writes a resume; it writes a *plan* for one: which facts to
show, in what order, and how each is phrased. Employers, titles and dates are
rendered from the facts themselves, so the model cannot change them. Every
bullet cites the fact it was rephrased from, which is what the validator checks
it against before anything is rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

VariantStatus = Literal["ready", "rejected"]


class Bullet(BaseModel):
    """One line on the page, rephrased from exactly one fact."""

    fact_id: int = Field(description="The id of the fact this line is drawn from.")
    text: str = Field(
        description=(
            "The line as it will appear. Reword and tighten the fact; never add a "
            "technology, employer, number or outcome the fact does not state."
        )
    )


class Entry(BaseModel):
    """One employer or project on the page: a heading fact and its bullets.

    The heading (title, employer, dates) comes from `fact_ids[0]`. Every bullet
    must cite one of `fact_ids`, and every fact in the list must describe the
    same employer and title.
    """

    fact_ids: list[int] = Field(description="Role or project facts that share one employer.")
    bullets: list[Bullet] = Field(description="Most relevant first. Three to five is typical.")


class TailoredResume(BaseModel):
    """The model's plan for one posting. Field order is page order."""

    summary: str = Field(
        default="",
        description=(
            "Two or three sentences, or empty. May only state what the facts state; "
            "it is checked against all of them."
        ),
    )
    skills: list[int] = Field(
        default_factory=list, description="Ids of skill facts to list, most relevant first."
    )
    experience: list[Entry] = Field(default_factory=list, description="Role entries, in order.")
    projects: list[Entry] = Field(default_factory=list, description="Project entries, in order.")
    education: list[int] = Field(default_factory=list, description="Ids of education facts.")
    credentials: list[int] = Field(default_factory=list, description="Ids of credential facts.")
    emphasis: str = Field(
        default="",
        description="One sentence on what this version leads with and why. Not rendered.",
    )

    def cited_fact_ids(self) -> list[int]:
        """Every fact id the plan refers to, in page order, without repeats."""
        seen: dict[int, None] = {}
        for entry in [*self.experience, *self.projects]:
            for fid in entry.fact_ids:
                seen.setdefault(fid, None)
            for bullet in entry.bullets:
                seen.setdefault(bullet.fact_id, None)
        for fid in [*self.skills, *self.education, *self.credentials]:
            seen.setdefault(fid, None)
        return list(seen)


class CoverLetter(BaseModel):
    """Three or four short paragraphs. Checked against the whole fact base."""

    paragraphs: list[str] = Field(description="Greeting through sign-off, one string each.")


@dataclass(slots=True)
class Issue:
    """One thing the validator will not let through."""

    where: str  # "summary", "experience[0].bullets[2]", "cover_letter[1]", ...
    message: str
    fact_id: int | None = None
    term: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "message": self.message,
            "fact_id": self.fact_id,
            "term": self.term,
        }


@dataclass(slots=True)
class Variant:
    """A tailored resume on disk, ready or rejected, with the numbers behind it."""

    job_id: str
    base_id: int | None
    content: TailoredResume
    status: VariantStatus = "ready"
    cover_letter: CoverLetter | None = None
    keyword_coverage: float | None = None
    base_coverage: float | None = None
    keywords: list[str] = field(default_factory=list)
    keywords_missing: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    pdf_path: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    attempts: int = 1
    id: int | None = None
    created_at: str | None = None

    @property
    def facts_used(self) -> list[int]:
        return self.content.cited_fact_ids()
