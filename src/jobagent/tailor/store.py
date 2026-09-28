"""Variants on disk. One row per attempt to tailor a resume to a job.

Rejected variants are kept too: the issues that sank them are the evidence the
guardrail works, and the dashboard shows them.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from jobagent.db.database import transaction, utcnow
from jobagent.tailor.models import CoverLetter, Issue, TailoredResume, Variant


def save_variant(conn: sqlite3.Connection, variant: Variant) -> int:
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO resume_variants
                   (job_id, base_id, facts_used, keyword_coverage, base_coverage, keywords,
                    keywords_missing, content, cover_letter, status, issues, pdf_path,
                    tokens_in, tokens_out, attempts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                variant.job_id,
                variant.base_id,
                json.dumps(variant.facts_used),
                variant.keyword_coverage,
                variant.base_coverage,
                json.dumps(variant.keywords),
                json.dumps(variant.keywords_missing),
                variant.content.model_dump_json(),
                variant.cover_letter.model_dump_json() if variant.cover_letter else None,
                variant.status,
                json.dumps([i.as_dict() for i in variant.issues]),
                variant.pdf_path,
                variant.tokens_in,
                variant.tokens_out,
                variant.attempts,
                utcnow(),
            ),
        )
    variant.id = int(cur.lastrowid)
    return variant.id


def set_pdf_path(conn: sqlite3.Connection, variant_id: int, pdf_path: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE resume_variants SET pdf_path = ? WHERE id = ?", (pdf_path, variant_id))


def _from_row(row: sqlite3.Row) -> Variant:
    issues = [Issue(**i) for i in json.loads(row["issues"] or "[]")]
    return Variant(
        id=row["id"],
        job_id=row["job_id"],
        base_id=row["base_id"],
        content=TailoredResume.model_validate_json(row["content"]),
        status=row["status"],
        cover_letter=(
            CoverLetter.model_validate_json(row["cover_letter"]) if row["cover_letter"] else None
        ),
        keyword_coverage=row["keyword_coverage"],
        base_coverage=row["base_coverage"],
        keywords=json.loads(row["keywords"] or "[]"),
        keywords_missing=json.loads(row["keywords_missing"] or "[]"),
        issues=issues,
        pdf_path=row["pdf_path"],
        tokens_in=row["tokens_in"] or 0,
        tokens_out=row["tokens_out"] or 0,
        attempts=row["attempts"] or 1,
        created_at=row["created_at"],
    )


def get_variant(conn: sqlite3.Connection, variant_id: int) -> Variant | None:
    row = conn.execute("SELECT * FROM resume_variants WHERE id = ?", (variant_id,)).fetchone()
    return _from_row(row) if row else None


def list_variants(
    conn: sqlite3.Connection, *, job_id: str | None = None, limit: int = 50
) -> list[Variant]:
    if job_id is None:
        rows = conn.execute(
            "SELECT * FROM resume_variants ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resume_variants WHERE job_id = ? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    return [_from_row(r) for r in rows]


def latest_ready_variant(conn: sqlite3.Connection, job_id: str) -> Variant | None:
    row = conn.execute(
        """SELECT * FROM resume_variants WHERE job_id = ? AND status = 'ready'
           ORDER BY id DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    return _from_row(row) if row else None


def variant_summary(variant: Variant) -> dict[str, Any]:
    """The row as the API returns it: everything but the full plan and letter."""
    return {
        "id": variant.id,
        "job_id": variant.job_id,
        "base_id": variant.base_id,
        "status": variant.status,
        "keyword_coverage": variant.keyword_coverage,
        "base_coverage": variant.base_coverage,
        "keywords": variant.keywords,
        "keywords_missing": variant.keywords_missing,
        "facts_used": variant.facts_used,
        "issues": [i.as_dict() for i in variant.issues],
        "has_cover_letter": variant.cover_letter is not None,
        "pdf_path": variant.pdf_path,
        "tokens_in": variant.tokens_in,
        "tokens_out": variant.tokens_out,
        "attempts": variant.attempts,
        "created_at": variant.created_at,
    }
