"""A compact view of the fact base for matching jobs against.

Two consumers: the rules scorer wants the candidate's keyword pool, and the
model classifier wants a short readable profile it can hold against a posting.
Both come from the fact base only, so a keyword the candidate never claimed
cannot earn a job points.
"""

from __future__ import annotations

import sqlite3

from jobagent.resume.facts import Fact, list_facts

_MAX_PROFILE_FACTS = 60


def profile_tags(conn: sqlite3.Connection) -> set[str]:
    """Every lowercase tag and skill name across active facts."""
    tags: set[str] = set()
    for fact in list_facts(conn):
        tags.update(fact.tags)
        if fact.kind == "skill":
            tags.add(fact.text.strip().lower())
    return tags


def _line(fact: Fact) -> str:
    detail = fact.detail
    if fact.kind in ("role", "project"):
        head = " · ".join(part for part in (detail.get("title"), detail.get("employer")) if part)
        dates = " – ".join(part for part in (detail.get("start"), detail.get("end")) if part)
        prefix = f"{head} ({dates})" if head and dates else head or ""
        return f"- {prefix}: {fact.text}" if prefix else f"- {fact.text}"
    return f"- {fact.text}"


def profile_text(conn: sqlite3.Connection) -> str:
    """The candidate as a short plain-text profile, grouped by kind."""
    facts = list_facts(conn)
    if not facts:
        return "(no resume on file)"

    groups: dict[str, list[Fact]] = {}
    for fact in facts:
        groups.setdefault(fact.kind, []).append(fact)

    order = ("summary", "role", "project", "skill", "education", "credential")
    sections: list[str] = []
    for kind in order:
        items = groups.get(kind)
        if not items:
            continue
        if kind == "skill":
            names = sorted({f.text.strip() for f in items}, key=str.lower)
            sections.append("Skills: " + ", ".join(names))
            continue
        heading = {
            "summary": "Summary",
            "role": "Experience",
            "project": "Projects",
            "education": "Education",
            "credential": "Credentials",
        }[kind]
        lines = [_line(f) for f in items[:_MAX_PROFILE_FACTS]]
        sections.append(f"{heading}:\n" + "\n".join(lines))

    return "\n\n".join(sections)
