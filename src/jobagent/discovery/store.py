"""Jobs on disk: dedupe on the way in, status on the way out.

A job's id is a hash of its canonical URL, title and company, so the same
posting seen twice collapses to one row (job-agent). The URL is also unique on
its own, so a posting re-listed under a slightly different title cannot be
applied to twice (jobpilot).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobagent.db.database import transaction, utcnow
from jobagent.discovery.ats import detect_ats
from jobagent.discovery.models import RawJob

STATUSES = ("pending", "skipped", "queued", "applied", "failed", "needs_review")

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gh_src",
    "lever-source",
    "ref",
    "refId",
    "trackingId",
    "src",
}


def canonical_url(url: str) -> str:
    """Strip tracking parameters and fragments so two links to one posting match."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def job_id(url: str, title: str, company: str) -> str:
    raw = f"{canonical_url(url)}|{title.strip().lower()}|{company.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(slots=True)
class UpsertResult:
    new_ids: list[str] = field(default_factory=list)
    duplicates: int = 0
    rejected: int = 0  # postings missing a url, title or company


def upsert_jobs(
    conn: sqlite3.Connection, jobs: Iterable[RawJob], run_id: int | None = None
) -> UpsertResult:
    """Insert what is new; count what is already known."""
    result = UpsertResult()
    now = utcnow()
    with transaction(conn):
        for job in jobs:
            if not (job.url and job.title and job.company):
                result.rejected += 1
                continue
            jid = job_id(job.url, job.title, job.company)
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                       (id, url, title, company, source, location, description, ats_type,
                        apply_url, salary_min, salary_max, posted_at, remote, external_id,
                        status, run_id, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    jid,
                    canonical_url(job.url),
                    job.title.strip(),
                    job.company.strip(),
                    job.source,
                    job.location,
                    job.description,
                    job.ats_type or detect_ats(job.apply_url or job.url),
                    job.apply_url,
                    job.salary_min,
                    job.salary_max,
                    job.posted_at,
                    None if job.remote is None else int(job.remote),
                    job.external_id,
                    run_id,
                    now,
                ),
            )
            if cur.rowcount == 1:
                result.new_ids.append(jid)
            else:
                result.duplicates += 1
    return result


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("remote") is not None:
        d["remote"] = bool(d["remote"])
    return d


def get_job(conn: sqlite3.Connection, jid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""SELECT * FROM jobs {where}
            ORDER BY COALESCE(tier, 9), COALESCE(score, -1) DESC, scraped_at DESC
            LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_jobs(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def set_status(conn: sqlite3.Connection, jid: str, status: str, reason: str | None = None) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with transaction(conn):
        if reason is None:
            cur = conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, jid))
        else:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, score_reason = ? WHERE id = ?", (status, reason, jid)
            )
    return cur.rowcount > 0


def set_description(conn: sqlite3.Connection, jid: str, description: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET description = ?, enriched_at = ? WHERE id = ?",
            (description, utcnow(), jid),
        )


def set_rule_score(conn: sqlite3.Connection, jid: str, score: int, reason: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET score = ?, score_reason = ?, scored_at = ? WHERE id = ?",
            (score, reason, utcnow(), jid),
        )


def set_classification(
    conn: sqlite3.Connection,
    jid: str,
    *,
    tier: int,
    fit_score: int,
    category: str | None,
    reason: str,
) -> None:
    with transaction(conn):
        conn.execute(
            """UPDATE jobs SET tier = ?, fit_score = ?, category = ?, tier_reason = ?,
                               classified_at = ? WHERE id = ?""",
            (tier, fit_score, category, reason, utcnow(), jid),
        )


def jobs_needing_enrichment(conn: sqlite3.Connection, min_chars: int = 400) -> list[dict[str, Any]]:
    """Pending jobs whose description is missing or looks like a snippet."""
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'pending' AND enriched_at IS NULL
             AND (description IS NULL OR LENGTH(description) < ?)""",
        (min_chars,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def jobs_pending_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending' AND scored_at IS NULL"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def jobs_pending_classification(conn: sqlite3.Connection, min_score: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'pending' AND scored_at IS NOT NULL AND classified_at IS NULL
             AND score >= ?
           ORDER BY score DESC""",
        (min_score,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ------------------------------------------------------------------- runs --


def start_run(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utcnow(),))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, **counts: int) -> None:
    allowed = {"found", "scored", "applied", "failed", "tokens_in", "tokens_out"}
    unknown = set(counts) - allowed
    if unknown:
        raise ValueError(f"unknown run counters: {sorted(unknown)}")
    sets = ", ".join(f"{k} = ?" for k in counts)
    with transaction(conn):
        conn.execute(
            f"UPDATE runs SET ended_at = ?{', ' + sets if sets else ''} WHERE id = ?",
            [utcnow(), *counts.values(), run_id],
        )


def add_run_tokens(conn: sqlite3.Connection, run_id: int, tokens_in: int, tokens_out: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id = ?",
            (tokens_in, tokens_out, run_id),
        )


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def as_json(value: Any) -> str:
    return json.dumps(value, default=str)
