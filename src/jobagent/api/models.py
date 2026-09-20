"""Request and response shapes for the dashboard API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FactKind = Literal["role", "project", "skill", "education", "credential", "summary"]


class FactOut(BaseModel):
    id: int
    kind: FactKind
    text: str
    detail: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source: Literal["parsed", "user_added"]
    active: bool


class FactIn(BaseModel):
    kind: FactKind = "skill"
    text: str
    detail: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class KeywordsIn(BaseModel):
    keywords: list[str] = Field(
        description="Skills or keywords to add to the fact base, as the candidate's own claims."
    )


class ActiveIn(BaseModel):
    active: bool


class NeverClaimIn(BaseModel):
    term: str
    note: str | None = None


class UploadOut(BaseModel):
    base_id: int
    filename: str
    facts_created: int
    already_uploaded: bool


class JobStatusIn(BaseModel):
    """A status a person may set by hand. `applied` and `failed` belong to the submitter."""

    status: Literal["pending", "queued", "skipped", "needs_review"]


class DiscoverOut(BaseModel):
    run_id: int
    report: dict[str, Any] | None = None
