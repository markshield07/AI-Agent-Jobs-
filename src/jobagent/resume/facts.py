"""The fact base: every claim a tailored resume is allowed to make.

A fact is either `parsed` out of an uploaded resume or `user_added` by the
candidate. Both are equally usable when tailoring — that is the point. Adding a
keyword is the candidate asserting it, not the model deciding it may.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jobagent.db.database import transaction, utcnow

KINDS = ("role", "project", "skill", "education", "credential", "summary")
SOURCES = ("parsed", "user_added")


@dataclass(slots=True)
class Fact:
    kind: str
    text: str
    detail: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    source: str = "parsed"
    id: int | None = None
    base_id: int | None = None
    active: bool = True

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Fact:
        return cls(
            id=row["id"],
            base_id=row["base_id"],
            kind=row["kind"],
            text=row["text"],
            detail=json.loads(row["detail"]) if row["detail"] else {},
            tags=json.loads(row["tags"]) if row["tags"] else [],
            source=row["source"],
            active=bool(row["active"]),
        )


def _normalise_tags(tags: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip().lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def add_fact(conn: sqlite3.Connection, fact: Fact) -> int:
    if fact.kind not in KINDS:
        raise ValueError(f"unknown fact kind {fact.kind!r}; expected one of {KINDS}")
    if fact.source not in SOURCES:
        raise ValueError(f"unknown fact source {fact.source!r}; expected one of {SOURCES}")
    if not fact.text.strip():
        raise ValueError("a fact needs text")

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO resume_facts
                   (base_id, kind, text, detail, tags, source, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact.base_id,
                fact.kind,
                fact.text.strip(),
                json.dumps(fact.detail) if fact.detail else None,
                json.dumps(_normalise_tags(fact.tags)),
                fact.source,
                int(fact.active),
                utcnow(),
            ),
        )
    return int(cur.lastrowid)


def add_facts(conn: sqlite3.Connection, facts: list[Fact]) -> list[int]:
    return [add_fact(conn, fact) for fact in facts]


def list_facts(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    source: str | None = None,
    include_inactive: bool = False,
) -> list[Fact]:
    clauses, params = [], []
    if not include_inactive:
        clauses.append("active = 1")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if source:
        clauses.append("source = ?")
        params.append(source)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM resume_facts {where} ORDER BY kind, id", params).fetchall()
    return [Fact.from_row(row) for row in rows]


def set_active(conn: sqlite3.Connection, fact_id: int, active: bool) -> bool:
    """Deactivate rather than delete: a variant already generated cites this id."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE resume_facts SET active = ? WHERE id = ?", (int(active), fact_id)
        )
    return cur.rowcount > 0


def add_keywords(conn: sqlite3.Connection, keywords: list[str]) -> list[int]:
    """Add plain skill keywords the candidate typed in.

    Skips anything already present as an active skill, so pasting the same list
    twice does not double it.
    """
    existing = {f.text.lower() for f in list_facts(conn, kind="skill")}
    created = []
    for keyword in keywords:
        cleaned = keyword.strip()
        if not cleaned or cleaned.lower() in existing:
            continue
        existing.add(cleaned.lower())
        created.append(
            add_fact(
                conn,
                Fact(kind="skill", text=cleaned, tags=[cleaned], source="user_added"),
            )
        )
    return created


# ----------------------------------------------------------- never-claim list --


def add_never_claim(conn: sqlite3.Connection, term: str, note: str | None = None) -> None:
    cleaned = term.strip()
    if not cleaned:
        raise ValueError("a never-claim term cannot be blank")
    with transaction(conn):
        conn.execute(
            """INSERT INTO never_claim (term, note, created_at) VALUES (?, ?, ?)
               ON CONFLICT(term) DO UPDATE SET note = excluded.note""",
            (cleaned, note, utcnow()),
        )


def list_never_claim(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM never_claim ORDER BY term").fetchall()
    return [{"term": r["term"], "note": r["note"]} for r in rows]


def remove_never_claim(conn: sqlite3.Connection, term: str) -> bool:
    with transaction(conn):
        cur = conn.execute("DELETE FROM never_claim WHERE term = ?", (term.strip(),))
    return cur.rowcount > 0


def violates_never_claim(conn: sqlite3.Connection, text: str) -> list[str]:
    """Return the never-claim terms that appear in `text`.

    Used by the tailoring stage in phase 3; exposed here so the fact base can be
    checked the moment a fact is added rather than at render time.
    """
    haystack = text.lower()
    return [entry["term"] for entry in list_never_claim(conn) if entry["term"].lower() in haystack]
