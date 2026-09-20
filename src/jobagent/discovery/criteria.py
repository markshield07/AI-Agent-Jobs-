"""What the candidate is looking for, and where to look.

Stored as one JSON document in the `settings` table so the dashboard can edit
it in place later. Every list is matched case-insensitively.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from jobagent.db.database import transaction, utcnow

SETTINGS_KEY = "search_criteria"

AtsName = Literal["greenhouse", "lever", "ashby"]
JobSpySite = Literal["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"]


class BoardRef(BaseModel):
    """A company's public ATS board, e.g. greenhouse + 'stripe'."""

    ats: AtsName
    slug: str
    company: str | None = Field(default=None, description="Display name; defaults to the slug.")


class SearchCriteria(BaseModel):
    titles: list[str] = Field(default_factory=list, description="Job titles to search for.")
    locations: list[str] = Field(default_factory=lambda: ["Remote"])
    remote_ok: bool = True
    keywords: list[str] = Field(
        default_factory=list,
        description="Terms that earn a posting points. The fact base's tags are added to these.",
    )
    exclude_keywords: list[str] = Field(
        default_factory=list, description="A posting mentioning any of these is rejected outright."
    )
    company_blacklist: list[str] = Field(default_factory=list)
    salary_min: int | None = None

    min_score: int = Field(default=60, ge=0, le=100, description="Rules score a posting needs.")
    max_tier: int = Field(default=2, ge=1, le=4, description="Worst model tier still queued.")
    max_age_hours: int = Field(default=72, ge=1)
    results_per_query: int = Field(default=50, ge=1, le=200)

    jobspy_sites: list[JobSpySite] = Field(default_factory=lambda: ["indeed", "linkedin"])
    boards: list[BoardRef] = Field(default_factory=list)

    def normalised(self, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v and v.strip()]


def load_criteria(conn: sqlite3.Connection) -> SearchCriteria:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return SearchCriteria()
    return SearchCriteria.model_validate_json(row["value"])


def save_criteria(conn: sqlite3.Connection, criteria: SearchCriteria) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (SETTINGS_KEY, criteria.model_dump_json(), utcnow()),
        )


def dump_criteria(criteria: SearchCriteria) -> dict:
    return json.loads(criteria.model_dump_json())
