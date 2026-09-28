"""What a job source hands back, before it is stored."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RawJob:
    """One posting as a source saw it.

    `description` is plain text when the source provides the full posting, or
    None when only a snippet was available; the enrichment stage fills the gap.
    `posted_at` is an ISO 8601 date or datetime string when the source states one.
    """

    url: str
    title: str
    company: str
    source: (
        str  # greenhouse | lever | ashby | indeed | linkedin | glassdoor | zip_recruiter | google
    )
    location: str | None = None
    description: str | None = None
    apply_url: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    posted_at: str | None = None
    remote: bool | None = None
    external_id: str | None = None
    ats_type: str | None = None
